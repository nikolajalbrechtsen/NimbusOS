#!/bin/bash
# =============================================================================
# tools/tests/docker-archive.test.sh — test for tools/fetch-docker-key.sh --verify-local
# =============================================================================
# What: copies the real build/config/archives/ files into a scratch copy of
#       the repository, checks that they pass, then breaks them one rule at a
#       time and checks that --verify-local rejects each break:
#         wrong fingerprint in a comment, docker.list.binary without signed-by,
#         signed-by pointing at another file, docker.list.chroot with
#         signed-by, a missing marker line, a second key in the key file, a
#         different key, a deb line for another suite. Works offline.
#
# Why:  CI relies on --verify-local to re-check Docker's key and the
#       fingerprint comments on every push (docs/PLAN.md §9, decision 3); the
#       check has to be able to fail.
#
# Usage: tools/tests/docker-archive.test.sh
#        Needs bash, gpg, gpg-agent and coreutils (Debian: tools/dev-shell.sh).
#        As root it installs gpg and gpg-agent itself if they are missing.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/nimbus-docker-archive-test.XXXXXXXX")"
trap 'rm -rf -- "$TMP"' EXIT
if ! command -v gpg >/dev/null 2>&1 || ! command -v gpg-agent >/dev/null 2>&1; then
	[[ "$(id -u)" -eq 0 ]] || { echo "docker-archive.test: needs gpg and gpg-agent" >&2; exit 2; }
	DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends gpg gpg-agent >/dev/null
fi

passed=0
failed=0

# fresh_copy — a minimal repository copy with the real files.
fresh_copy() {
	rm -rf -- "$TMP/repo"
	mkdir -p "$TMP/repo/build/lib" "$TMP/repo/build/config/archives" "$TMP/repo/tools"
	cp "$ROOT/build/lib/common.sh" "$TMP/repo/build/lib/"
	cp "$ROOT/build/nimbus.conf" "$TMP/repo/build/"
	cp "$ROOT"/build/config/archives/docker.* "$TMP/repo/build/config/archives/"
	cp "$ROOT/tools/fetch-docker-key.sh" "$TMP/repo/tools/"
}
A="$TMP/repo/build/config/archives"
run() { bash "$TMP/repo/tools/fetch-docker-key.sh" --verify-local >"$TMP/out" 2>&1; }

expect_pass() {
	if run; then passed=$((passed + 1)); printf '  PASS  %s\n' "$1"
	else failed=$((failed + 1)); printf '  FAIL  %s was rejected:\n%s\n' "$1" "$(sed 's/^/        /' "$TMP/out")"; fi
}
expect_reject() {
	if run; then failed=$((failed + 1)); printf '  FAIL  %s was accepted\n' "$1"
	elif grep -q -- "$2" "$TMP/out"; then passed=$((passed + 1)); printf '  PASS  %s is rejected\n' "$1"
	else failed=$((failed + 1)); printf '  FAIL  %s was rejected, but not for "%s":\n%s\n' "$1" "$2" "$(sed 's/^/        /' "$TMP/out")"; fi
}

# A second, unrelated key for the key-file cases.
export GNUPGHOME="$TMP/gnupg"
mkdir -m 0700 "$GNUPGHOME"
gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
	--quick-gen-key "Unrelated <unrelated@example.invalid>" ed25519 sign 1d
gpg --batch --quiet --armor --export unrelated@example.invalid >"$TMP/other.asc"
gpgconf --kill gpg-agent || true
unset GNUPGHOME

echo "Docker archive files (tools/fetch-docker-key.sh --verify-local)"
fresh_copy
expect_pass "the committed files"

fresh_copy
sed -i 's/9DC8 5822 9FC7 DD38 854A  E2D8 8D81 803C 0EBF CD88/9DC8 5822 9FC7 DD38 854A  E2D8 8D81 803C 0EBF CD89/' "$A/docker.list.binary"
expect_reject "a wrong fingerprint comment" "states fingerprint"

fresh_copy
sed -i 's/ signed-by=@NIMBUS_DOCKER_KEYRING_PATH@//' "$A/docker.list.binary"
expect_reject "docker.list.binary without signed-by" "does not use signed-by"

fresh_copy
sed -i 's/signed-by=@NIMBUS_DOCKER_KEYRING_PATH@/signed-by=@NIMBUS_DOCKER_KEYRING_PATH@.old/' "$A/docker.list.binary"
expect_reject "signed-by naming a different file" "does not use signed-by"

fresh_copy
sed -i 's/\[arch=@NIMBUS_ARCH@\]/[arch=@NIMBUS_ARCH@ signed-by=\/etc\/apt\/keyrings\/docker.asc]/' "$A/docker.list.chroot"
expect_reject "docker.list.chroot with signed-by" "must not use signed-by"

fresh_copy
sed -i '/^# nimbus:docker.list.chroot$/d' "$A/docker.list.chroot"
expect_reject "a missing marker line" "lacks its marker line"

fresh_copy
cat "$TMP/other.asc" >>"$A/docker.key.chroot"
expect_reject "a second key in the key file" "does not contain exactly Docker's key"

fresh_copy
cp "$TMP/other.asc" "$A/docker.key.chroot"
expect_reject "a different key" "does not contain exactly Docker's key"

fresh_copy
sed -i 's/@NIMBUS_DEBIAN_SUITE@ stable/bookworm stable/' "$A/docker.list.binary"
expect_reject "a deb line for another suite" "is not https://download.docker.com/linux/debian"

echo
printf 'docker-archive.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
