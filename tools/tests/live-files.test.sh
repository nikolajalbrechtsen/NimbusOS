#!/bin/bash
# =============================================================================
# tools/tests/live-files.test.sh — tests for includes.chroot_after_packages
# =============================================================================
# What: checks the live-session-only files, rendered as build.sh renders
#       them, in the current (throwaway) container:
#         Calamares   every file is valid YAML; every module settings.conf
#                     names exists in Trixie's calamares or
#                     calamares-settings-debian package (or is one of its own
#                     instances); each instance's config file exists
#         launchers   nimbus-install.desktop and the autostart entry pass
#                     desktop-file-validate; the installer launcher runs
#                     Debian's calamares-install-debian
#         mounts      both tmpfs units only mount with boot=live, before
#                     Docker/containerd, and are enabled into local-fs.target
#         sources-final  replaces the container's Debian source with
#                     deb822 debian.sources (main contrib non-free
#                     non-free-firmware, trixie, -updates, -security, Debian's
#                     keyring); sources.list keeps only comments; apt-get
#                     update works without a warning and fetches all 12
#                     suite/component pairs;
#                     a second run changes nothing; without the keyring it
#                     fails clearly
#         grub-debconf  without a GRUB package does nothing; with grub-pc,
#                     records the disk whose MBR holds GRUB as
#                     grub-pc/install_devices (by-id name, never a partition;
#                     else the device path); no GRUB there or no answer from
#                     grub-probe: records nothing and still exits 0; with
#                     grub-efi-amd64, sets force_efi_extra_removable, runs
#                     grub-install --force-extra-removable --no-nvram and
#                     copies EFI/<id>/grub.cfg into EFI/BOOT, and a failure
#                     doesn't stop the install; runs right after
#                     Calamares' bootloader step, whose own EFI fallback is off
#         live-session-setup  outside a live session does nothing; in one
#                     puts an executable copy of "Install NimbusOS" on the
#                     desktop, once
#         target-cleanup  after hooks 0300 and 0400 (as in the image), removes
#                     every listed file, disables both mounts (no link left),
#                     removes /usr/lib/nimbusos-live, keeps a listed file that
#                     belongs to a package; without the list it fails
#
# Why:  docs/PLAN.md NOTE-06, NOTE-21, NOTE-63, NOTE-72. These files decide what an
#       installed system looks like, but run only inside the installer.
#
# Usage: tools/tests/live-files.test.sh
#        Network (deb.debian.org). Throwaway container only.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
[[ "$(id -u)" -eq 0 ]] || { echo "live-files.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "live-files.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }
[[ ! -e /live-build && ! -e /usr/lib/nimbusos-live ]] \
	|| { echo "live-files.test: use a fresh container" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"
nimbus_load_conf

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
WORK=""
nimbus_mktemp_dir WORK

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends python3 python3-yaml desktop-file-utils systemd >"$WORK/deps.log" 2>&1 \
	|| { tail -n 30 "$WORK/deps.log"; exit 1; }
INC="$WORK/includes"
cp -a "$ROOT/build/config/includes.chroot_after_packages" "$INC"
nimbus_render_tree "$INC"
HOOKS="$WORK/hooks"
cp -a "$ROOT/build/config/hooks/live" "$HOOKS"
nimbus_render_tree "$HOOKS"

# --- Calamares ------------------------------------------------------------------------
echo "Calamares configuration"
(cd "$WORK" && apt-get download -q calamares calamares-settings-debian >/dev/null 2>&1)
for d in "$WORK"/calamares*.deb; do dpkg-deb -x "$d" "$WORK/cal"; done
for f in "$INC"/etc/calamares/settings.conf "$INC"/etc/calamares/modules/*.conf; do
	check "valid YAML: ${f#"$INC"}" python3 -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' "$f"
done
check "every module in settings.conf exists in Trixie's Calamares" python3 - "$INC/etc/calamares/settings.conf" "$WORK/cal" <<'EOF'
import os, sys, yaml
s = yaml.safe_load(open(sys.argv[1]))
root = sys.argv[2]
known = set()
for d in ("usr/lib/x86_64-linux-gnu/calamares/modules", "usr/lib/calamares/modules"):
    p = os.path.join(root, d)
    if os.path.isdir(p):
        known |= set(os.listdir(p))
instances = {i["id"]: i["module"] for i in s.get("instances", [])}
missing = []
for block in s["sequence"]:
    for kind, mods in block.items():
        for m in mods:
            name = instances[m.split("@", 1)[1]] if "@" in m else m
            if name not in known:
                missing.append(m)
if missing:
    print("unknown modules:", missing)
    sys.exit(1)
EOF
check "every instance's config file exists" python3 - "$INC" <<'EOF'
import os, sys, yaml
inc = sys.argv[1]
s = yaml.safe_load(open(os.path.join(inc, "etc/calamares/settings.conf")))
sys.exit(0 if all(os.path.isfile(os.path.join(inc, "etc/calamares/modules", i["config"])) for i in s["instances"]) else 1)
EOF

echo "Launchers"
check "nimbus-install.desktop passes desktop-file-validate" desktop-file-validate "$INC/usr/share/applications/nimbus-install.desktop"
check "nimbus-live-session.desktop passes desktop-file-validate" desktop-file-validate "$INC/etc/xdg/autostart/nimbus-live-session.desktop"
check "\"Install ${NIMBUS_NAME}\" runs Debian's calamares-install-debian (in Trixie's package)" \
	sh -c 'grep -qx "Exec=calamares-install-debian" "$1" && [ -x "$2/usr/bin/calamares-install-debian" ]' _ \
	"$INC/usr/share/applications/nimbus-install.desktop" "$WORK/cal"

echo "Live-only mounts"
for unit in var-lib-docker.mount var-lib-containerd.mount; do
	u="$INC/etc/systemd/system/$unit"
	check "$unit mounts only with boot=live" grep -qx 'ConditionKernelCommandLine=boot=live' "$u"
	check "$unit: Where= matches the unit name" test "$(systemd-escape --path --suffix=mount "$(sed -n 's/^Where=//p' "$u")")" = "$unit"
	check "$unit: tmpfs, before containerd/Docker, WantedBy=local-fs.target" \
		sh -c 'grep -qx "Type=tmpfs" "$1" && grep -q "^Before=.*docker.service" "$1" && grep -qx "WantedBy=local-fs.target" "$1"' _ "$u"
done

# --- sources-final -----------------------------------------------------------------------
echo "sources-final"
SF="$INC/usr/lib/nimbusos-live/sources-final"
check "runs" sh "$SF"
DS=/etc/apt/sources.list.d/debian.sources
check "debian.sources: two deb822 stanzas, Debian's keyring" \
	sh -c '[ "$(grep -c "^Types: deb$" "$1")" = 2 ] && [ "$(grep -c "^Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp$" "$1")" = 2 ]' _ "$DS"
check "sources.list holds comments only" sh -c '! grep -v "^#" /etc/apt/sources.list | grep -q .'
apt-get update >"$WORK/update.log" 2>&1 && rc=0 || rc=$?
check "apt-get update works with the new sources" test "$rc" = 0
check "  ... without a warning or note (every component exists, signatures check out)" \
	sh -c '! grep -qE "^(W|E|N):" "$1"' _ "$WORK/update.log"
# What apt fetches, not what it stored: apt keeps no file for an index that is
# empty right now (no contrib update yet, for example), so apt-cache policy
# and apt-get indextargets leave such suites out.
targets="$(apt-get update --print-uris 2>/dev/null | grep -oE "dists/[a-z-]+/[a-z-]+/binary-[a-z0-9]+/Packages" \
	| sed -E 's|^dists/||; s|/binary-.*$||' | sort -u)"
for suite in "${NIMBUS_DEBIAN_SUITE}" "${NIMBUS_DEBIAN_SUITE}-updates" "${NIMBUS_DEBIAN_SUITE}-security"; do
	for comp in $NIMBUS_ARCHIVE_AREAS; do
		check "apt fetches $suite/$comp" grep -qx "$suite/$comp" <<<"$targets"
	done
done
check "no backports" sh -c '! grep -q backports "$1"' _ "$DS"
sums="$(sha256sum "$DS" /etc/apt/sources.list)"
check "a second run changes nothing" sh -c 'sh "$1" >/dev/null && [ "$(sha256sum "$2" /etc/apt/sources.list)" = "$3" ]' _ "$SF" "$DS" "$sums"
mkdir -p "$WORK/fakeroot/etc/apt/sources.list.d"
check "fails clearly without Debian's keyring" \
	sh -c '! sh "$1" "$2" >"$3" 2>&1 && grep -q "debian-archive-keyring.pgp is missing" "$3"' _ "$SF" "$WORK/fakeroot" "$WORK/sf-bad.out"

# --- grub-debconf (NOTE-72) ------------------------------------------------------------------
echo "grub-debconf"
GD="$INC/usr/lib/nimbusos-live/grub-debconf"
devices() { echo "get grub-pc/install_devices" | debconf-communicate grub-pc 2>/dev/null | sed -n 's/^0 //p'; }
check "without a GRUB package: does nothing, exits 0" \
	sh -c '"$1" >"$2" 2>&1 && grep -q "nothing to do" "$2"' _ "$GD" "$WORK/gd-none.out"
apt-get install -y -q --no-install-recommends grub-pc >"$WORK/grub-pc.log" 2>&1 || { tail -n 30 "$WORK/grub-pc.log"; exit 1; }
# Disks are image files here; a stand-in grub-probe names the one in
# NIMBUS_TEST_DISK, and a by-id directory of our own links to them.
mkdir -p "$WORK/stub" "$WORK/by-id"
printf '#!/bin/sh\n[ -n "${NIMBUS_TEST_DISK:-}" ] && echo "$NIMBUS_TEST_DISK"\n' >"$WORK/stub/grub-probe"
chmod +x "$WORK/stub/grub-probe"
for d in grub nogrub unlinked; do truncate -s 1M "$WORK/$d.img"; done
for d in grub unlinked; do dd if=/usr/lib/grub/i386-pc/boot.img of="$WORK/$d.img" conv=notrunc status=none; done
ln -s "$WORK/grub.img" "$WORK/by-id/ata-NIMBUS_TEST-part1"     # sorts first, and is a partition
ln -s "$WORK/grub.img" "$WORK/by-id/virtio-NIMBUS-TEST"
ln -s "$WORK/nogrub.img" "$WORK/by-id/virtio-NIMBUS-EMPTY"
gd() {   # gd DISK [fail]: run grub-debconf with the stand-ins
	env PATH="$WORK/stub:$PATH" NIMBUS_BY_ID_DIR="$WORK/by-id" NIMBUS_ESP="$WORK/esp" \
		NIMBUS_TEST_DISK="$1" NIMBUS_TEST_FAIL="${2:-}" "$GD" >"$WORK/gd.out" 2>&1
}
reset_devices() { echo "grub-pc grub-pc/install_devices multiselect " | debconf-set-selections; }
reset_devices
gd "$WORK/grub.img" && rc=0 || rc=$?
check "BIOS, GRUB in the disk's MBR: records the disk by its by-id name, not a partition's" \
	test "$rc:$(devices)" = "0:$WORK/by-id/virtio-NIMBUS-TEST"
reset_devices
gd "$WORK/nogrub.img" && rc=0 || rc=$?
check "the boot loader went elsewhere (no GRUB in that MBR): records nothing, says so, exits 0" \
	sh -c '[ "$1" = 0 ] && [ -z "$2" ] && grep -q "doesn.t hold GRUB" "$3"' _ "$rc" "$(devices)" "$WORK/gd.out"
gd "$WORK/unlinked.img" && rc=0 || rc=$?
check "a disk without a by-id name: records its device path" test "$rc:$(devices)" = "0:$WORK/unlinked.img"
reset_devices
gd "" && rc=0 || rc=$?
check "grub-probe fails: records nothing, says so, exits 0 (never stops the install)" \
	sh -c '[ "$1" = 0 ] && [ -z "$2" ] && grep -q "grub-probe can.t name" "$3"' _ "$rc" "$(devices)" "$WORK/gd.out"
# UEFI: grub-efi-amd64 replaces grub-pc (they conflict); a stand-in grub-install
# records its arguments, or fails when NIMBUS_TEST_FAIL is set.
apt-get install -y -q --no-install-recommends grub-efi-amd64 >"$WORK/grub-efi.log" 2>&1 || { tail -n 30 "$WORK/grub-efi.log"; exit 1; }
printf '#!/bin/sh\necho "$*" >"%s"\n[ -z "${NIMBUS_TEST_FAIL:-}" ]\n' "$WORK/grub-install.args" >"$WORK/stub/grub-install"
chmod +x "$WORK/stub/grub-install"
removable() { echo "get grub2/force_efi_extra_removable" | debconf-communicate grub-efi-amd64 2>/dev/null | sed -n 's/^0 //p'; }
echo "grub-efi-amd64 grub2/force_efi_extra_removable boolean false" | debconf-set-selections
# The EFI system partition as grub-install leaves it: the small grub.cfg only
# next to GRUB in EFI/<id>.
mkdir -p "$WORK/esp/EFI/$NIMBUS_ID" "$WORK/esp/EFI/BOOT"
printf "search.fs_uuid 0123 root\nset prefix=(\$root)'/boot/grub'\nconfigfile \$prefix/grub.cfg\n" >"$WORK/esp/EFI/$NIMBUS_ID/grub.cfg"
gd "" && rc=0 || rc=$?
check "UEFI: GRUB's updates keep the fallback path (force_efi_extra_removable true)" test "$rc:$(removable)" = "0:true"
check "UEFI: runs grub-install as grub-efi-amd64 does, with --force-extra-removable and --no-nvram" \
	test "$(cat "$WORK/grub-install.args")" = "--target=x86_64-efi --force-extra-removable --no-nvram"
check "UEFI: EFI/BOOT gets the grub.cfg from EFI/${NIMBUS_ID} (GRUB reads the one next to it)" \
	cmp -s "$WORK/esp/EFI/$NIMBUS_ID/grub.cfg" "$WORK/esp/EFI/BOOT/grub.cfg"
rm -f "$WORK/esp/EFI/BOOT/grub.cfg"
gd "" fail && rc=0 || rc=$?
check "UEFI: a failing grub-install is reported, copies nothing, and the install goes on (exit 0)" \
	sh -c '[ "$1" = 0 ] && grep -q "force-extra-removable failed" "$2" && [ ! -e "$3" ]' _ "$rc" "$WORK/gd.out" "$WORK/esp/EFI/BOOT/grub.cfg"
check "Calamares runs it right after its bootloader step" python3 - "$INC/etc/calamares/settings.conf" <<'EOF'
import sys, yaml
s = yaml.safe_load(open(sys.argv[1]))
execs = [m for block in s["sequence"] for kind, mods in block.items() if kind == "exec" for m in mods]
sys.exit(0 if execs[execs.index("bootloader") + 1] == "shellprocess@nimbus-grub-debconf" else 1)
EOF
check "Calamares' own EFI fallback (GRUB alone in EFI/BOOT) is off" \
	grep -qx 'installEFIFallback: false' "$INC/etc/calamares/modules/bootloader.conf"

# --- the live files in place, as in the image -----------------------------------------------
cp -a "$INC/." /
mkdir -p /live-build/config
cp -a "$INC" /live-build/config/includes.chroot_after_packages
bash "$HOOKS/0300-live-units.hook.chroot" >"$WORK/0300.out" 2>&1
bash "$HOOKS/0400-live-manifest.hook.chroot" >"$WORK/0400.out" 2>&1

echo "live-session-setup"
export HOME="$WORK/home"
mkdir -p "$HOME"
check "outside a live session: does nothing" \
	sh -c '/usr/lib/nimbusos-live/live-session-setup && [ ! -e "$HOME/Desktop" ]'
mkdir -p /run/live/medium
check "in a live session: \"Install ${NIMBUS_NAME}\" on the desktop, executable" \
	sh -c '/usr/lib/nimbusos-live/live-session-setup && [ -x "$HOME/Desktop/nimbus-install.desktop" ] && cmp -s "$HOME/Desktop/nimbus-install.desktop" /usr/share/applications/nimbus-install.desktop'
touch -d '2026-01-01' "$HOME/Desktop/nimbus-install.desktop"
check "a second login leaves it alone" \
	sh -c '/usr/lib/nimbusos-live/live-session-setup && [ "$(stat -c %Y "$HOME/Desktop/nimbus-install.desktop")" = "$(date -d 2026-01-01 +%s)" ]'
rmdir /run/live/medium

echo "target-cleanup"
LIST=/usr/lib/nimbusos-live/live-files
check "before: the mounts are enabled and listed" \
	sh -c '[ "$(systemctl is-enabled var-lib-docker.mount)" = enabled ] && grep -qx /etc/systemd/system/var-lib-docker.mount "$1"' _ "$LIST"
mapfile -t listed < <(grep '^/' "$LIST")
echo /etc/issue.net >>"$LIST"           # a listed file that belongs to base-files
cp /usr/lib/nimbusos-live/target-cleanup "$WORK/target-cleanup"
check "runs (from /usr/lib/nimbusos-live, as Calamares runs it)" sh -c '/usr/lib/nimbusos-live/target-cleanup >"$1" 2>&1' _ "$WORK/cleanup.out"
gone=yes
for f in "${listed[@]}"; do [[ ! -e "$f" ]] || { gone="no ($f)"; break; }; done
check "every listed live file is gone (${#listed[@]} files)" test "$gone" = yes
check "no link to either mount is left" \
	sh -c '[ -z "$(find /etc/systemd/system -name "var-lib-docker.mount" -o -name "var-lib-containerd.mount")" ]'
check "/usr/lib/nimbusos-live is removed" test ! -e /usr/lib/nimbusos-live
check "a listed file that belongs to a package is kept, and said so" \
	sh -c '[ -s /etc/issue.net ] && grep -q "keeping /etc/issue.net: it belongs to base-files" "$1"' _ "$WORK/cleanup.out"
check "reports what it did" grep -q "disabled var-lib-containerd.mount var-lib-docker.mount\|disabled var-lib-docker.mount var-lib-containerd.mount" "$WORK/cleanup.out"
check "without the list it fails (an image built without hook 0400)" \
	sh -c '! "$1" >"$2" 2>&1 && grep -q "live-files is missing" "$2"' _ "$WORK/target-cleanup" "$WORK/cleanup-bad.out"

echo
echo "--- target-cleanup output ---"
cat "$WORK/cleanup.out"
echo "-----------------------------"
printf 'live-files.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
