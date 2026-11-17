#!/bin/bash
# =============================================================================
# tools/tests/image-apt-check.test.sh — test for the final-image APT check
# =============================================================================
# What: builds small squashfs images that imitate the finished live image, one
#       correct and one for each failure the check must catch, and runs
#       nimbus_check_image_apt (build/lib/common.sh) against each. The
#       sources are the real build/config/archives files, rendered as
#       build.sh renders them:
#         good       docker.list from docker.list.binary, Signed-By on every
#                    line, Docker's key only in the packaged keyring
#         failures   build-time docker.list.chroot left in place; a deb line
#                    without Signed-By; the build-only key left in
#                    trusted.gpg.d; Docker's key trusted globally under
#                    another name; a second Docker source; the packaged
#                    keyring missing.
#       A throwaway key plays the part of Docker's key (NIMBUS_DOCKER_KEY_FPR
#       is overridden for this test only).
#
# Why:  build.sh runs this check on every ISO (docs/PLAN.md NOTE-03); a check
#       that never fails would be worse than none.
#
# Usage: tools/tests/image-apt-check.test.sh
#        Runs on Debian: needs gpg, mksquashfs and unsquashfs (squashfs-tools).
#        As root it installs missing tools itself (tools/dev-shell.sh).
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"

# A throwaway "Docker" key, made before common.sh is loaded so that its
# fingerprint can stand in for Docker's.
KEYHOME="$(mktemp -d "${TMPDIR:-/tmp}/nimbus-image-test-gpg.XXXXXXXX")"
if ! command -v gpg >/dev/null 2>&1 || ! command -v gpg-agent >/dev/null 2>&1 \
	|| ! command -v mksquashfs >/dev/null 2>&1; then
	[[ "$(id -u)" -eq 0 ]] || { echo "image-apt-check.test: needs gpg, gpg-agent and squashfs-tools" >&2; exit 2; }
	DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends gpg gpg-agent squashfs-tools >/dev/null
fi
GNUPGHOME="$KEYHOME" gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
	--quick-gen-key "Docker stand-in <docker-standin@example.invalid>" ed25519 sign 1d
GNUPGHOME="$KEYHOME" gpg --batch --quiet --armor --export docker-standin@example.invalid >"$KEYHOME/docker.asc"
NIMBUS_DOCKER_KEY_FPR="$(GNUPGHOME="$KEYHOME" gpg --batch --with-colons --fingerprint docker-standin@example.invalid \
	| awk -F: '$1 == "fpr" { print $10; exit }')"
export NIMBUS_DOCKER_KEY_FPR
GNUPGHOME="$KEYHOME" gpgconf --kill gpg-agent || true

# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"
_NIMBUS_TMPDIRS+=("$KEYHOME")
nimbus_load_conf

passed=0
failed=0
nimbus_mktemp_dir WORK

# render_real FILE DEST — one of the real build/config/archives files, with
# its @NIMBUS_*@ tokens filled in exactly as build.sh does it.
render_real() {
	cp -- "$ROOT/build/config/archives/$1" "$2"
	nimbus_render_tokens "$2"
}

# make_image NAME — builds $WORK/NAME.squashfs from the "good" layout (the
# real docker.list.binary as live-build installs it) after running the
# function mutate_NAME (if defined) on it.
make_image() {
	local name="$1" root="$WORK/$1"
	mkdir -p "$root/etc/apt/sources.list.d" "$root/etc/apt/trusted.gpg.d" "$root/usr/share/keyrings"
	render_real docker.list.binary "$root/etc/apt/sources.list.d/docker.list"
	printf 'deb %s %s main\n' "$NIMBUS_MIRROR" "$NIMBUS_DEBIAN_SUITE" >"$root/etc/apt/sources.list"
	GNUPGHOME="$KEYHOME" gpg --batch --quiet --dearmor <"$KEYHOME/docker.asc" >"$root$NIMBUS_DOCKER_KEYRING_PATH"
	if declare -F "mutate_$name" >/dev/null; then
		"mutate_$name" "$root"
	fi
	mksquashfs "$root" "$WORK/$name.squashfs" -noappend -quiet -no-progress >/dev/null
}

mutate_chroot_list_left() {
	# As if live-build had left the build-time source in place.
	render_real docker.list.chroot "$1/etc/apt/sources.list.d/docker.list"
}
mutate_line_without_signed_by() {
	printf 'deb %s %s stable\n' "$NIMBUS_DOCKER_APT_URL" "$NIMBUS_DEBIAN_SUITE" >>"$1/etc/apt/sources.list.d/docker.list"
}
mutate_build_key_left() {
	cp "$KEYHOME/docker.asc" "$1/etc/apt/trusted.gpg.d/docker.key.chroot.asc"
}
mutate_key_trusted_globally() {
	cp "$KEYHOME/docker.asc" "$1/etc/apt/trusted.gpg.d/vendor-extra.asc"
}
mutate_second_docker_source() {
	printf 'deb %s %s stable\n' "$NIMBUS_DOCKER_APT_URL" "$NIMBUS_DEBIAN_SUITE" >"$1/etc/apt/sources.list.d/extra.list"
}
mutate_keyring_missing() {
	rm -f "$1$NIMBUS_DOCKER_KEYRING_PATH"
}

# expect_ok NAME / expect_fail NAME PHRASE
expect_ok() {
	make_image "$1"
	if (nimbus_check_image_apt "$WORK/$1.squashfs") >"$WORK/$1.out" 2>&1; then
		passed=$((passed + 1)); printf '  PASS  %s is accepted\n' "$1"
	else
		failed=$((failed + 1)); printf '  FAIL  %s was rejected:\n%s\n' "$1" "$(sed 's/^/        /' "$WORK/$1.out")"
	fi
}
expect_fail() {
	make_image "$1"
	if (nimbus_check_image_apt "$WORK/$1.squashfs") >"$WORK/$1.out" 2>&1; then
		failed=$((failed + 1)); printf '  FAIL  %s was accepted\n' "$1"
	elif grep -q -- "$2" "$WORK/$1.out"; then
		passed=$((passed + 1)); printf '  PASS  %s is rejected (%s)\n' "$1" "$2"
	else
		failed=$((failed + 1)); printf '  FAIL  %s was rejected, but not for "%s":\n%s\n' "$1" "$2" "$(sed 's/^/        /' "$WORK/$1.out")"
	fi
}

echo "Final-image APT check (nimbus_check_image_apt)"
expect_ok   good
expect_fail chroot_list_left        "is not the docker.list.binary source"
expect_fail line_without_signed_by  "line without signed-by"
expect_fail build_key_left          "build-only key left in the image"
expect_fail key_trusted_globally    "globally trusted"
expect_fail second_docker_source    "adds a second Docker source"
expect_fail keyring_missing         "is missing"

echo
printf 'image-apt-check.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
