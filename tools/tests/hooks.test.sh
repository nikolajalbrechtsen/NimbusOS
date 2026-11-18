#!/bin/bash
# =============================================================================
# tools/tests/hooks.test.sh — tests for build/config/hooks/live/*.hook.chroot
# =============================================================================
# What: runs each NimbusOS live-build hook, rendered as build.sh renders it,
#       in the current (throwaway) container, which stands in for the image
#       build chroot: no systemd, Debian's cleanup hooks already done (empty
#       machine-id), and /live-build/config set up the way lb chroot_hooks
#       bind-mounts it.
#         0100  adds Flathub with the packaged setup, again is a no-op,
#               leaves no file behind, fails clearly without its setup script
#         0200  hides Trixie's real "Install Debian" entries (from the
#               calamares-settings-debian package), valid per
#               desktop-file-validate, idempotent, replaces an existing value,
#               fails if Debian renames a file
#         0300  enables every unit in includes.chroot_after_packages, fails
#               if one didn't reach the chroot, accepts none; with the real
#               includes it enables the Docker and containerd tmpfs mounts
#         0400  lists every live-session file and itself; refuses a live
#               file owned by a package that stays installed, and one that
#               didn't reach the chroot
#         0900  in this partial image, with the real live files in place:
#               passes what is right (os-release, NimbusOS packages, groups,
#               theme, installer configuration, live-file list, launchers,
#               Flathub, APT, tokens, machine-id), fails what is missing here
#               (Plasma and other list packages, four services) with exactly
#               those names, and fails each deliberate breakage with a
#               message naming it.
#       Also: the hook's package list equals NIMBUS_PACKAGES, and no token is
#       left after rendering.
#
# Why:  docs/PLAN.md NOTE-62. A verify hook that never fails, or fails on a
#       good image, is worse than none.
#
# Usage: tools/tests/hooks.test.sh DEBS_DIR
#        DEBS_DIR holds nimbusos-archive-keyring, nimbusos-docker-keyring,
#        nimbusos-release, nimbusos-branding and nimbus-control .debs from
#        tools/build-debs.sh. Network (deb.debian.org, dl.flathub.org).
#        Throwaway container only (tools/dev-shell.sh).
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
DEBS="$(readlink -f "${1:?usage: hooks.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "hooks.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "hooks.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }
[[ ! -e /live-build ]] || { echo "hooks.test: /live-build already exists; use a fresh container" >&2; exit 2; }
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
HOOKS="$WORK/hooks"
cp -a "$ROOT/build/config/hooks/live" "$HOOKS"
nimbus_render_tree "$HOOKS"
run_hook() {   # run_hook NAME: run the rendered hook; output in $WORK/NAME.out
	bash "$HOOKS/$1.hook.chroot" >"$WORK/$1.out" 2>&1
}
out() { cat "$WORK/$1.out"; }
has() { grep -qF -- "$2" "$WORK/$1.out"; }      # has HOOK TEXT
line() { grep -F -- "$2" "$WORK/$1.out" | head -n 1; }

echo "Hook files"
check "0900's package list is NIMBUS_PACKAGES" \
	test "$(sed -n 's/^NIMBUS_PACKAGES="\(.*\)"$/\1/p' "$HOOKS/0900-verify-image.hook.chroot" | tr ' ' '\n' | sort)" \
	= "$(printf '%s\n' "${NIMBUS_PACKAGES[@]}" | sort)"
check "no token is left after rendering" sh -c '! grep -rE "@NIMBUS_[A-Z0-9_]+@" "$1"' _ "$HOOKS"
for h in "$HOOKS"/*.hook.chroot; do
	check "bash -n $(basename "$h")" bash -n "$h"
done

# --- a partial NimbusOS system --------------------------------------------------------
version="$(dpkg-deb -f "$(find "$DEBS" -name 'nimbusos-release_*.deb' -print -quit)" Version)"
stand_in() {   # stand_in NAME VERSION: an empty package
	local d="$WORK/stand-in-$1"
	rm -rf "$d"; mkdir -p "$d/DEBIAN"
	printf 'Package: %s\nVersion: %s\nArchitecture: all\nMaintainer: NimbusOS tests <tests@invalid>\nDescription: stand-in for %s (hooks tests)\n' "$1" "$2" "$1" >"$d/DEBIAN/control"
	dpkg-deb -b --root-owner-group "$d" "$WORK/$1.deb" >/dev/null
	dpkg -i "$WORK/$1.deb" >/dev/null
}
apt-get update -q >/dev/null
for p in docker-ce docker-ce-cli docker-compose-plugin chromium qemu-system-x86 ovmf swtpm-tools; do
	stand_in "$p" "99:0+stand.in"
done
mapfile -t debs < <(find "$DEBS" -maxdepth 1 \( -name 'nimbusos-archive-keyring_*.deb' -o -name 'nimbusos-docker-keyring_*.deb' \
	-o -name 'nimbusos-release_*.deb' -o -name 'nimbusos-branding_*.deb' -o -name 'nimbus-control_*.deb' \) -name "*_${version}_*.deb")
((${#debs[@]} == 5)) || { echo "hooks.test: need 5 NimbusOS .debs of version $version in $DEBS, found ${#debs[@]}" >&2; exit 2; }
apt-get install -y -q --no-install-recommends "${debs[@]}" flatpak desktop-file-utils >"$WORK/install.log" 2>&1 \
	|| { tail -n 30 "$WORK/install.log"; exit 1; }
stand_in nimbus-catalog "$version"     # deliverable 9
stand_in nimbusos-desktop "$version"   # its Depends need the whole desktop
: >/etc/machine-id                      # as Debian's hooks 8020 and 8060 leave it
rm -f /var/lib/dbus/machine-id

# --- 0100 ------------------------------------------------------------------------------------
echo "0100-flathub-preseed"
snapshot() { find /tmp /var/tmp /root -mindepth 1 2>/dev/null | grep -v "^$WORK" | LC_ALL=C sort; }
before="$(snapshot)"
check "adds Flathub" sh -c 'bash "$1" >"$2" 2>&1 && flatpak remotes --system --columns=name | grep -qx flathub' _ \
	"$HOOKS/0100-flathub-preseed.hook.chroot" "$WORK/0100.out"
check "writes the stamp, so the boot-time unit skips itself" test -e /var/lib/nimbusos/flathub-setup-done
check "leaves no file in /tmp, /var/tmp or /root" test "$(snapshot)" = "$before"
check "leaves /etc/machine-id empty" test ! -s /etc/machine-id
check "a second run is a no-op" run_hook 0100-flathub-preseed
mv /usr/lib/nimbusos/nimbus-flathub-setup "$WORK/setup.saved"
check "fails clearly without nimbusos-release's setup script" \
	sh -c '! bash "$1" >"$2" 2>&1 && grep -q "nimbus-flathub-setup is missing" "$2"' _ "$HOOKS/0100-flathub-preseed.hook.chroot" "$WORK/0100-bad.out"
mv "$WORK/setup.saved" /usr/lib/nimbusos/nimbus-flathub-setup

# --- 0200 ------------------------------------------------------------------------------------
echo "0200-live-installer-launcher"
APP=/usr/share/applications/calamares-install-debian.desktop
AUTO=/etc/xdg/autostart/calamares-desktop-icon.desktop
(cd "$WORK" && apt-get download -q calamares-settings-debian >/dev/null)
dpkg-deb --fsys-tarfile "$WORK"/calamares-settings-debian_*.deb | tar -x -C / ".$APP" ".$AUTO"
entry_value() {   # entry_value FILE KEY: KEY's values in the [Desktop Entry] group
	awk -v k="$2" '/^\[/ { e = ($0 == "[Desktop Entry]") } e && index($0, k "=") == 1 { print substr($0, length(k) + 2) }' "$1"
}
check "Trixie's files are the ones the hook expects" test -f "$APP" -a -f "$AUTO"
validate() { desktop-file-validate "$APP" "$AUTO" 2>&1 | LC_ALL=C sort || true; }
valid_before="$(validate)"
check "runs" run_hook 0200-live-installer-launcher
check "\"Install Debian\" has NoDisplay=true" test "$(entry_value "$APP" NoDisplay)" = true
check "the desktop-icon autostart has Hidden=true" test "$(entry_value "$AUTO" Hidden)" = true
check "desktop-file-validate: no new message compared with Debian's originals" test "$(validate)" = "$valid_before"
echo "        (Debian's own files already report: $(tr '\n' ' ' <<<"$valid_before"))"
sums="$(sha256sum "$APP" "$AUTO")"
check "a second run changes nothing" sh -c 'bash "$1" >/dev/null 2>&1 && [ "$(sha256sum "$2" "$3")" = "$4" ]' _ \
	"$HOOKS/0200-live-installer-launcher.hook.chroot" "$APP" "$AUTO" "$sums"
sed -i 's/^NoDisplay=true$/NoDisplay=false/' "$APP"
check "replaces an existing NoDisplay=false (one line, true)" \
	sh -c 'bash "$1" >/dev/null 2>&1 && [ "$(grep -c "^NoDisplay=" "$2")" = 1 ] && grep -qx NoDisplay=true "$2"' _ \
	"$HOOKS/0200-live-installer-launcher.hook.chroot" "$APP"
mv "$AUTO" "$WORK/auto.saved"
check "fails if Debian renames a file" \
	sh -c '! bash "$1" >"$2" 2>&1 && grep -q "calamares-settings-debian has changed" "$2"' _ \
	"$HOOKS/0200-live-installer-launcher.hook.chroot" "$WORK/0200-bad.out"
mv "$WORK/auto.saved" "$AUTO"

# --- 0300 ------------------------------------------------------------------------------------
echo "0300-live-units"
INC=/live-build/config/includes.chroot_after_packages/etc/systemd/system
mkdir -p "$INC"
check "no live-only units: nothing to do" sh -c 'bash "$1" | grep -q "no live-only units"' _ "$HOOKS/0300-live-units.hook.chroot"
printf '[Unit]\nDescription=tmpfs for hooks.test\n[Mount]\nWhat=tmpfs\nWhere=/var/lib/nimbustest\nType=tmpfs\n[Install]\nWantedBy=local-fs.target\n' \
	>"$INC/var-lib-nimbustest.mount"
check "fails if a unit didn't reach the chroot" \
	sh -c '! bash "$1" >"$2" 2>&1 && grep -q "var-lib-nimbustest.mount is missing in the chroot" "$2"' _ \
	"$HOOKS/0300-live-units.hook.chroot" "$WORK/0300-bad.out"
cp "$INC/var-lib-nimbustest.mount" /etc/systemd/system/
check "enables every unit from includes.chroot_after_packages" \
	sh -c 'bash "$1" >/dev/null 2>&1 && [ "$(systemctl is-enabled var-lib-nimbustest.mount)" = enabled ] && [ -L /etc/systemd/system/local-fs.target.wants/var-lib-nimbustest.mount ]' _ \
	"$HOOKS/0300-live-units.hook.chroot"
systemctl --quiet disable var-lib-nimbustest.mount
rm -f /etc/systemd/system/var-lib-nimbustest.mount "$INC/var-lib-nimbustest.mount"

# --- the real live-session files, as lb chroot_includes_after_packages copies them -----------
echo "0300 and 0400 with the real includes.chroot_after_packages"
INCLUDES="$WORK/includes"
cp -a "$ROOT/build/config/includes.chroot_after_packages" "$INCLUDES"
nimbus_render_tree "$INCLUDES"
rm -rf /live-build/config/includes.chroot_after_packages
cp -a "$INCLUDES" /live-build/config/includes.chroot_after_packages
cp -a "$INCLUDES/." /
check "0300 enables the live-only Docker and containerd mounts" \
	sh -c 'bash "$1" >/dev/null 2>&1 && [ "$(systemctl is-enabled var-lib-docker.mount)" = enabled ] && [ "$(systemctl is-enabled var-lib-containerd.mount)" = enabled ]' _ \
	"$HOOKS/0300-live-units.hook.chroot"
check "0400 lists every live-session file and itself" \
	sh -c 'bash "$1" >/dev/null 2>&1 && [ "$(grep -c "^/" /usr/lib/nimbusos-live/live-files)" = "$(( $(find "$2" -type f | wc -l) + 1 ))" ] && grep -qx /usr/lib/nimbusos-live/live-files /usr/lib/nimbusos-live/live-files' _ \
	"$HOOKS/0400-live-manifest.hook.chroot" "$INCLUDES"
echo "overridden by the live image" >/live-build/config/includes.chroot_after_packages/etc/issue.net
check "0400 refuses a live file that belongs to a package that stays installed" \
	sh -c '! bash "$1" >"$2" 2>&1 && grep -q "/etc/issue.net belongs to base-files, which stays on installed systems" "$2"' _ \
	"$HOOKS/0400-live-manifest.hook.chroot" "$WORK/0400-owned.out"
rm /live-build/config/includes.chroot_after_packages/etc/issue.net
mkdir -p /live-build/config/includes.chroot_after_packages/usr/lib/nimbusos-live
echo x >/live-build/config/includes.chroot_after_packages/usr/lib/nimbusos-live/not-copied
check "0400 refuses a live file that didn't reach the chroot" \
	sh -c '! bash "$1" >"$2" 2>&1 && grep -q "not-copied is in the includes but not in the chroot" "$2"' _ \
	"$HOOKS/0400-live-manifest.hook.chroot" "$WORK/0400-missing.out"
rm /live-build/config/includes.chroot_after_packages/usr/lib/nimbusos-live/not-copied
run_hook 0400-live-manifest

# --- 0900 ------------------------------------------------------------------------------------
echo "0900-verify-image (this partial image)"
mkdir -p /live-build/config/package-lists
cp "$ROOT"/build/config/package-lists/*.list.chroot /live-build/config/package-lists/
V=0900-verify-image
check "fails (a partial image is not a NimbusOS image)" sh -c '! bash "$1" >"$2" 2>&1' _ "$HOOKS/$V.hook.chroot" "$WORK/$V.out"
for name in os-release nimbusos-packages desktop-metapackage groups nimbus-control-user adduser-conf \
	boot-splash desktop-theme sddm-theme installer live-files installer-launcher flathub apt-sources tokens machine-id; do
	check "ok: $name" grep -qx "  ok    $name" "$WORK/$V.out"
done
check "FAIL package-lists names what is missing here (plasma-desktop, sddm, calamares)" \
	sh -c 'l=$(grep "^  FAIL  package-lists: not installed: " "$1") && for p in plasma-desktop sddm calamares; do echo "$l" | grep -qw "$p" || exit 1; done' _ "$WORK/$V.out"
check "FAIL package-lists does not name installed packages (flatpak, samba)" \
	bash -c '! grep "^  FAIL  package-lists:" "$1" | tr " " "\n" | grep -qxE "flatpak|samba"' _ "$WORK/$V.out"
svc="$(line "$V" "  FAIL  services: ")"
for u in display-manager.service NetworkManager.service docker.service containerd.service; do
	check "FAIL services names $u" grep -qF "$u is" <<<"$svc"
done
check "FAIL services explains the display manager" grep -qF "display-manager.service is not set (SDDM's postinst sets it)" <<<"$svc"
for u in nimbus-control.service nimbus-privd.socket nimbus-update-auto.timer nimbus-flathub-setup.service \
	smbd.service wsdd2.service avahi-daemon.service libvirtd.service unattended-upgrades.service apt-daily-upgrade.timer \
	var-lib-docker.mount var-lib-containerd.mount; do
	check "services: $u (enabled here) is not named" bash -c '! grep -qF "$1 is" <<<"$2"' _ "$u" "$svc"
done
check "nmbd off is accepted" bash -c '! grep -qF "nmbd" <<<"$1"' _ "$svc"
check "exactly 2 checks failed" has "$V" "2 check(s) failed"
check "live-user groups missing here only warn" grep -q "^  warn  live-user-groups: " "$WORK/$V.out"

echo "0900-verify-image (deliberate breakages)"
# expect_fail NAME TEXT: the verify hook fails check NAME with TEXT in its message
expect_fail() {
	bash "$HOOKS/$V.hook.chroot" >"$WORK/$V.mut.out" 2>&1 || true
	grep -F "  FAIL  $1: " "$WORK/$V.mut.out" | grep -qF -- "$2"
}
cp -p /usr/lib/os-release "$WORK/os-release.saved"
sed -i 's/^ID=.*/ID=debian/' /usr/lib/os-release
check "os-release with ID=debian" expect_fail os-release "ID=debian (want nimbusos)"
cp -p "$WORK/os-release.saved" /usr/lib/os-release

gpasswd -d nimbus-control libvirt >/dev/null
check "nimbus-control not in libvirt" expect_fail nimbus-control-user "not in group(s): libvirt"
gpasswd -a nimbus-control libvirt >/dev/null

sed -i -E "s/^REQUIRED_GROUPS=\"(.*)\"$/REQUIRED_GROUPS=\"\1 nimbus-test-missing\"/" "$HOOKS/$V.hook.chroot"
check "a required group that doesn't exist" expect_fail groups "missing: nimbus-test-missing"
sed -i -E "s/ nimbus-test-missing\"$/\"/" "$HOOKS/$V.hook.chroot"

sed -i 's/^ADD_EXTRA_GROUPS=1$/ADD_EXTRA_GROUPS=0/' /etc/adduser.conf
check "ADD_EXTRA_GROUPS=0" expect_fail adduser-conf "ADD_EXTRA_GROUPS is not 1"
sed -i 's/^ADD_EXTRA_GROUPS=0$/ADD_EXTRA_GROUPS=1/' /etc/adduser.conf

stand_in nimbus-catalog "0.0.1~other"
check "a NimbusOS package of another version" expect_fail nimbusos-packages "versions differ"
stand_in nimbus-catalog "$version"

plymouth-set-default-theme text
check "Plymouth theme text" expect_fail boot-splash "is 'text', not nimbusos"
plymouth-set-default-theme nimbusos

update-alternatives --set desktop-theme /usr/share/desktop-base/ceratopsian-theme >/dev/null
check "desktop-theme set to ceratopsian" expect_fail desktop-theme "ceratopsian-theme"
update-alternatives --auto desktop-theme >/dev/null

mv /var/lib/nimbusos/flathub-setup-done "$WORK/stamp.saved"
check "no Flathub stamp" expect_fail flathub "no stamp"
mv "$WORK/stamp.saved" /var/lib/nimbusos/flathub-setup-done

key="$(sed -n 's/^Signed-By:[[:space:]]*//p' /etc/apt/sources.list.d/nimbusos.sources | head -n 1)"
mv "$key" "$WORK/key.saved"
check "the NimbusOS keyring missing" expect_fail apt-sources "Signed-By keyring"
mv "$WORK/key.saved" "$key"

cp -p /usr/lib/nimbusos/nimbus-flathub-setup "$WORK/setup.saved"
printf '# %s\n' "@NIMBUS_""HOME_URL@" >>/usr/lib/nimbusos/nimbus-flathub-setup
check "an unfilled token in a NimbusOS file" expect_fail tokens "/usr/lib/nimbusos/nimbus-flathub-setup"
cp -p "$WORK/setup.saved" /usr/lib/nimbusos/nimbus-flathub-setup

echo 0123456789abcdef0123456789abcdef >/etc/machine-id
check "a machine-id in the image" expect_fail machine-id "/etc/machine-id is not empty"
: >/etc/machine-id

systemctl --quiet enable nmbd.service
check "nmbd enabled" expect_fail services "nmbd.service is enabled"
systemctl --quiet disable nmbd.service

# break_file FILE SED_EXPRESSION: edit FILE (a copy is restored by restore_file)
break_file() { cp -p "$1" "$WORK/broken.saved"; sed -i -e "$2" "$1"; }
restore_file() { cp -p "$WORK/broken.saved" "$1"; }
CM=/etc/calamares/modules
break_file /etc/calamares/settings.conf '/shellprocess@nimbus-target-cleanup$/d'
check "installer without the target cleanup step" expect_fail installer "does not run shellprocess@nimbus-target-cleanup"
restore_file /etc/calamares/settings.conf
break_file /etc/calamares/settings.conf 's/^branding: .*/branding: debian/'
check "installer with Debian's branding" expect_fail installer "uses branding 'debian'"
restore_file /etc/calamares/settings.conf
break_file /etc/calamares/settings.conf 's/^  - shellprocess@nimbus-sources-final$/  - sources-final/'
check "installer back on Debian's sources-final" expect_fail installer "still runs Debian's sources-final"
restore_file /etc/calamares/settings.conf
break_file "$CM/users.conf" 's/name: nimbus-control$/name: nimbus-other/'
check "users.conf without nimbus-control" expect_fail installer "does not add new users to nimbus-control"
restore_file "$CM/users.conf"
break_file "$CM/users.conf" '$a broken: [yaml'
check "users.conf that is not YAML" expect_fail installer "users.conf is not valid YAML"
restore_file "$CM/users.conf"
break_file "$CM/bootloader.conf" 's/^efiBootloaderId: .*/efiBootloaderId: "debian"/'
check "an EFI directory that isn't GRUB_DISTRIBUTOR" expect_fail installer "differs from GRUB_DISTRIBUTOR ('${NIMBUS_ID}')"
restore_file "$CM/bootloader.conf"
break_file "$CM/packages.conf" "/'calamares'\$/d"
check "Calamares left on installed systems" expect_fail installer "does not remove calamares;"
restore_file "$CM/packages.conf"
mv /usr/lib/nimbusos-live/live-files "$WORK/live-files.saved"
check "no live-file list" expect_fail live-files "is missing (hook 0400)"
mv "$WORK/live-files.saved" /usr/lib/nimbusos-live/live-files
break_file "$APP" 's/^NoDisplay=true$/NoDisplay=false/'
check "Debian's \"Install Debian\" visible" expect_fail installer-launcher "is not hidden (hook 0200)"
restore_file "$APP"

mkdir -p "$WORK/lists" && mv /live-build/config/package-lists/*.list.chroot "$WORK/lists/"
printf 'flatpak\nsamba\n# a comment\n#! nimbus: desktop-recommends\nplymouth\n' >/live-build/config/package-lists/10-test.list.chroot
check "package lists that are all installed pass" \
	sh -c 'bash "$1" >"$2" 2>&1; grep -qx "  ok    package-lists" "$2"' _ "$HOOKS/$V.hook.chroot" "$WORK/$V.lists.out"
rm /live-build/config/package-lists/10-test.list.chroot && mv "$WORK/lists"/*.list.chroot /live-build/config/package-lists/

check "after restoring everything: the same 2 failures as before" \
	sh -c 'bash "$1" >"$2" 2>&1; grep -q "2 check(s) failed" "$2"' _ "$HOOKS/$V.hook.chroot" "$WORK/$V.final.out"

echo
echo "--- 0900-verify-image output in this partial image (lists shortened) ---"
sed -E 's/(not installed: ([^ ]+ ){6}).*/\1…/' "$WORK/$V.out"
echo "-------------------------------------------------------------------------"
printf 'hooks.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
