#!/bin/bash
# =============================================================================
# tools/tests/image-initrd-check.test.sh — test for the boot-splash check
# =============================================================================
# What: builds real initramfs images with Trixie's kernel, initramfs-tools
#       and Plymouth, the way live-build's chroot_hacks does after the hooks
#       (update-initramfs -k all -t -u), and runs nimbus_check_image_initrd
#       (build/lib/common.sh) against them:
#         - with nimbusos-branding installed: the check passes;
#         - with the Plymouth theme switched to "text" and the initramfs
#           rebuilt: it fails and names the missing theme files;
#         - back to nimbusos: it passes again;
#         - a file that is not an initramfs, and a missing file: it fails.
#
# Why:  the NimbusOS boot splash depends on the theme being in the live
#       initramfs. live-build regenerates that initramfs after every hook,
#       so build.sh checks the result instead of running a hook of its own
#       (docs/PLAN.md NOTE-62).
#
# Usage: tools/tests/image-initrd-check.test.sh DEBS_DIR
#        DEBS_DIR holds the nimbusos-branding .deb. Throwaway container only
#        (tools/dev-shell.sh): it installs a kernel and packages.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
DEBS="$(readlink -f "${1:?usage: image-initrd-check.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "image-initrd-check.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "image-initrd-check.test: refusing to run outside a container (it installs a kernel)" >&2; exit 2; }
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
# run_check INITRD: the check in a subshell (it exits on failure); output kept.
run_check() { (nimbus_check_image_initrd "$1") >"$WORK/check.out" 2>&1; }
says() { grep -qF -- "$1" "$WORK/check.out"; }
fails() { ! "$@"; }

deb="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-branding_*.deb' -print -quit)"
[[ -n "$deb" ]] || { echo "image-initrd-check.test: no nimbusos-branding .deb in $DEBS" >&2; exit 2; }
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends linux-image-amd64 initramfs-tools initramfs-tools-core plymouth "$deb" \
	>"$WORK/install.log" 2>&1 || { tail -n 30 "$WORK/install.log"; exit 1; }
rebuild() { update-initramfs -k all -t -u >"$WORK/update-initramfs.log" 2>&1; }
rebuild
initrd="$(find /boot -maxdepth 1 -name 'initrd.img-*' -print -quit)"
[[ -n "$initrd" ]] || { echo "image-initrd-check.test: update-initramfs made no /boot/initrd.img-*" >&2; exit 1; }
echo "Kernel $(basename "$initrd" | sed 's/^initrd.img-//'), Plymouth theme $(plymouth-set-default-theme)"

check "nimbusos theme selected: the check passes" run_check "$initrd"
check "  ... and says so" says "Image initrd check OK"

plymouth-set-default-theme text
rebuild
check "theme text: the check fails" fails run_check "$initrd"
run_check "$initrd" || true
check "  ... naming the missing theme files" says "usr/share/plymouth/themes/nimbusos/nimbusos.plymouth is not in the initramfs"
check "  ... and the likely cause" says "Is nimbusos-branding installed and its Plymouth theme selected?"

plymouth-set-default-theme nimbusos
rebuild
check "back to nimbusos: the check passes again" run_check "$initrd"

head -c 4096 /dev/urandom >"$WORK/not-an-initrd"
check "a file that is not an initramfs fails" fails run_check "$WORK/not-an-initrd"
check "a missing file fails" fails run_check "$WORK/nothing-here"
run_check "$WORK/nothing-here" || true
check "  ... saying it is missing" says "is missing or empty"

echo
printf 'image-initrd-check.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
