#!/bin/bash
# =============================================================================
# tools/tests/bootloaders.test.sh — the live boot menus, as live-build makes them
# =============================================================================
# What: runs live-build's own binary_grub_cfg, binary_syslinux and
#       binary_bootloader_splash (live-build 1:20250505) on a minimal tree:
#       the real build/config rendered as build.sh renders it, the GRUB theme
#       from the built nimbusos-branding, lb config with NimbusOS's own
#       arguments, and an empty kernel/initrd pair. Then checks the result:
#         GRUB (UEFI)    the three "Start NimbusOS" entries with the right
#                        kernel, initrd and options (safe graphics adds
#                        nomodeset; fail-safe keeps the live user settings),
#                        no "Live system" entry, no leftover token, 10 s
#                        timeout, every cfg passes grub-script-check, the
#                        fonts theme.cfg loads exist in live-theme/
#         ISOLINUX (BIOS) the same three entries, title, 10 s timeout, no
#                        leftover token
#         splash         640x480 (ISOLINUX) and 800x600 (GRUB) PNGs rendered
#                        from config/bootloaders/splash.svg, version filled in
#       The PNGs are copied to dist/preview/ (or OUT_DIR) for a look.
#
# Why:  docs/PLAN.md NOTE-64. live-build rewrites these files with sed; only
#       its own scripts show what really ends up on the ISO.
#
# Usage: tools/tests/bootloaders.test.sh DEBS_DIR [OUT_DIR]
#        DEBS_DIR holds the nimbusos-branding .deb. Throwaway container only.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
DEBS="$(readlink -f "${1:?usage: bootloaders.test.sh DEBS_DIR [OUT_DIR]}")"
OUT="${2:-$ROOT/dist/preview}"
[[ "$(id -u)" -eq 0 ]] || { echo "bootloaders.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "bootloaders.test: refusing to run outside a container (it installs packages)" >&2; exit 2; }
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
has() { grep -qF -- "$2" "$1"; }          # has FILE TEXT
hasnt() { ! grep -qE -- "$2" "$1"; }      # hasnt FILE REGEX

branding="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-branding_*.deb' -print -quit)"
[[ -n "$branding" ]] || { echo "bootloaders.test: no nimbusos-branding .deb in $DEBS" >&2; exit 2; }
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends live-build grub-common isolinux syslinux-common \
	librsvg2-bin fonts-inter fonts-dejavu-core file >/tmp/bootloaders-deps.log 2>&1 \
	|| { tail -n 30 /tmp/bootloaders-deps.log; exit 1; }

WORK=""
nimbus_mktemp_dir WORK
cd "$WORK"
cp -R --preserve=mode,timestamps -- "$ROOT/build/config" config
nimbus_render_tree config
nimbus_prepare_grub_theme "$WORK/config" "$branding" >/dev/null 2>&1
nimbus_lb_config_args "0.2.0"
lb config "${NIMBUS_LB_ARGS[@]}" --build-with-chroot false >lb-config.log 2>&1 \
	|| { tail -n 20 lb-config.log; exit 1; }
# The smallest tree the three scripts need: kernel and initrd names, the
# os-release the splash reads, and the stages they require.
KVER=6.12.0-9-amd64
mkdir -p chroot/boot chroot/etc binary/live .build
for f in "vmlinuz-$KVER" "initrd.img-$KVER"; do
	: >"chroot/boot/$f"
	: >"binary/live/$f"      # where binary_linux-image would have copied them
done
printf 'NAME="%s"\nVERSION_ID="0.2.0"\n' "$NIMBUS_NAME" >chroot/etc/os-release
touch .build/config .build/bootstrap
for script in binary_grub_cfg binary_syslinux binary_bootloader_splash; do
	lb "$script" --force >"$script.log" 2>&1 || { echo "lb $script failed:"; tail -n 20 "$script.log"; exit 1; }
done

G=binary/boot/grub
I=binary/isolinux
LIVE="boot=live components ${NIMBUS_BOOT_EXTRA} hostname=${NIMBUS_HOSTNAME} username=${NIMBUS_LIVE_USERNAME}"
USERSET="username=${NIMBUS_LIVE_USERNAME} user-fullname=${NIMBUS_LIVE_USER_FULLNAME} user-default-groups=${NIMBUS_LIVE_USER_GROUPS}"

echo "GRUB (UEFI)"
check "three Start ${NIMBUS_NAME} entries" test "$(grep -c "^menuentry \"Start ${NIMBUS_NAME}" "$G/grub.cfg")" = 3
check "normal entry: kernel, live options, findiso" \
	has "$G/grub.cfg" "linux	/live/vmlinuz-$KVER $LIVE"
check "normal entry: findiso for booting the ISO file from disk" has "$G/grub.cfg" 'findiso=${iso_path}'
check "initrd /live/initrd.img-$KVER" has "$G/grub.cfg" "initrd	/live/initrd.img-$KVER"
check "safe graphics adds nomodeset" grep -qE "^	linux	/live/vmlinuz-$KVER boot=live .* nomodeset$" "$G/grub.cfg"
check "fail-safe keeps the live user and its groups" \
	grep -qF "linux	/live/vmlinuz-$KVER boot=live components hostname=${NIMBUS_HOSTNAME} $USERSET" "$G/grub.cfg"
check "fail-safe has live-build's fail-safe options and no splash" \
	sh -c 'grep "fail-safe" -A1 "$1" | grep -q "memtest noapic noapm nodma nomce nosmp nosplash vga=788" && ! grep "fail-safe" -A1 "$1" | grep -q " splash"' _ "$G/grub.cfg"
check "no live-build \"Live system\" entry" sh -c '! grep -v "^[[:space:]]*#" "$1" | grep -q "Live system"' _ "$G/grub.cfg"
check "no token left in any GRUB cfg" sh -c '! grep -E "@[A-Z_]+@" "$@"' _ "$G"/*.cfg
check "starts the first entry after 10 seconds" has "$G/grub.cfg" 'set timeout=10'
check "UEFI firmware settings in Utilities" has "$G/grub.cfg" 'fwsetup'
for cfg in "$G"/*.cfg; do
	check "grub-script-check $(basename "$cfg")" grub-script-check "$cfg"
done
while read -r font; do
	check "theme.cfg font exists: $font" test -s "binary$font"
done < <(sed -n 's/^loadfont \(.*\)$/\1/p' "$G/theme.cfg")
check "theme.cfg loads three fonts" test "$(grep -c '^loadfont ' "$G/theme.cfg")" = 3
check "the nimbusos theme is on the medium" test -s "$G/live-theme/theme.txt" -a -s "$G/live-theme/background.png"

echo "ISOLINUX (BIOS)"
check "three Start ${NIMBUS_NAME} entries" test "$(grep -c "menu label .*Start ${NIMBUS_NAME}" "$I/live.cfg")" = 3
check "normal entry: kernel and live options" \
	sh -c 'grep -q "^	linux /live/vmlinuz$" "$1" && grep -qF "append $2" "$1"' _ "$I/live.cfg" "$LIVE"
check "safe graphics adds nomodeset" grep -qE '^	append boot=live .* nomodeset$' "$I/live.cfg"
check "fail-safe keeps the live user and its groups" grep -qF "$USERSET" "$I/live.cfg"
check "the first entry is the default" sh -c 'awk "/^label/{n++} /menu default/{print n}" "$1" | grep -qx 1' _ "$I/live.cfg"
check "no live-build \"Live system\" entry" sh -c '! cat "$@" | grep -v "^[[:space:]]*#" | grep -q "Live system"' _ "$I"/*.cfg
check "no token left in any ISOLINUX cfg" sh -c '! grep -E "@[A-Z_]+@" "$@"' _ "$I"/*.cfg
check "menu title is ${NIMBUS_NAME}" has "$I/menu.cfg" "menu title ${NIMBUS_NAME}"
check "10-second timeout" grep -qx 'timeout 100' "$I/isolinux.cfg"

echo "Splash"
check "ISOLINUX splash.png is 640 x 480" sh -c 'file "$1" | grep -q "PNG image data, 640 x 480"' _ "$I/splash.png"
check "GRUB splash.png is 800 x 600" sh -c 'file "$1" | grep -q "PNG image data, 800 x 600"' _ "$G/splash.png"
mkdir -p "$OUT"
cp "$I/splash.png" "$OUT/splash-isolinux-640x480.png"
cp "$G/splash.png" "$OUT/splash-grub-800x600.png"
echo "  (previews: $OUT/splash-isolinux-640x480.png, splash-grub-800x600.png)"

echo
printf 'bootloaders.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
