#!/bin/bash
# =============================================================================
# tools/tests/deb-version.test.sh — regression test for tools/deb-version.sh
# =============================================================================
# What: builds a throwaway git repository with fixed commit dates, tags it
#       the way NimbusOS releases are tagged, and checks every mapping from
#       git state to Debian version, suite, channel and display version, plus
#       the error cases. Version ordering is checked with the real
#       `dpkg --compare-versions`.
#
# Why:  the tag-to-version mapping decides upgrade order on every installed
#       system (docs/PLAN.md NOTE-32); a mistake there would strand users on
#       a beta or block upgrades, so it must be exact.
#
# Usage: tools/tests/deb-version.test.sh
#        Runs on Debian (dpkg and git required): CI, or tools/dev-shell.sh.
#        Exit status 0 when every check passes.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/nimbus-deb-version-test.XXXXXXXX")"
trap 'rm -rf -- "$TMP"' EXIT

for tool in git dpkg; do
	command -v "$tool" >/dev/null 2>&1 || { echo "deb-version.test: $tool is required" >&2; exit 2; }
done

passed=0
failed=0

pass() { passed=$((passed + 1)); printf '  PASS  %s\n' "$1"; }
fail() { failed=$((failed + 1)); printf '  FAIL  %s\n        expected: %s\n        actual:   %s\n' "$1" "$2" "$3"; }

# expect NAME EXPECTED ACTUAL — exact match.
expect() { if [[ "$3" == "$2" ]]; then pass "$1"; else fail "$1" "$2" "$3"; fi; }
# expect_re NAME REGEX ACTUAL — regular-expression match.
expect_re() { if [[ "$3" =~ $2 ]]; then pass "$1"; else fail "$1" "/$2/" "$3"; fi; }
# expect_order NAME A OP B — real dpkg version comparison.
expect_order() {
	if dpkg --compare-versions "$2" "$3" "$4"; then pass "$1"; else fail "$1" "$2 $3 $4" "not true"; fi
}

# A repository that contains only what deb-version.sh needs, isolated from
# the user's git configuration, with fixed identity and dates so commit ids
# and versions are reproducible.
REPO="$TMP/repo"
mkdir -p "$REPO/build/lib" "$REPO/tools"
cp "$ROOT/build/lib/common.sh" "$REPO/build/lib/"
cp "$ROOT/tools/deb-version.sh" "$REPO/tools/"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="NimbusOS Test" GIT_AUTHOR_EMAIL="test@example.invalid"
export GIT_COMMITTER_NAME="NimbusOS Test" GIT_COMMITTER_EMAIL="test@example.invalid"
export GIT_AUTHOR_DATE="2026-11-17T12:00:00+0000" GIT_COMMITTER_DATE="2026-11-17T12:00:00+0000"
EPOCH="$(date -u -d "2026-11-17T12:00:00Z" +%s)"
git -C "$REPO" init -q -b main
git -C "$REPO" config commit.gpgsign false
git -C "$REPO" config tag.gpgsign false

commit() { printf '%s\n' "$1" >"$REPO/f"; git -C "$REPO" add -A; git -C "$REPO" commit -q -m "$1"; }
tag()    { git -C "$REPO" tag "$1"; }
# dv: stdout and stderr together, for checking error messages.
# field: stdout only, so expected warnings don't end up in the value.
dv()     { bash "$REPO/tools/deb-version.sh" "$@" 2>&1; }
field()  { bash "$REPO/tools/deb-version.sh" --field "$1" "${@:2}" 2>/dev/null; }

SHA_RE='g[0-9a-f]{7}'

echo "Untagged history"
commit A
expect_re "no tags: version"      "^0\.0\.0\+git20261117\.1\.${SHA_RE}$" "$(field version)"
expect    "no tags: channel"      "dev"        "$(field channel)"
expect    "no tags: suite"        "UNRELEASED" "$(field suite)"
expect    "no tags: tag"          ""           "$(field tag)"
expect    "no tags: epoch"        "$EPOCH"     "$(field epoch)"

echo "Stable release"
tag v0.1.0
expect "v0.1.0: version"          "0.1.0"  "$(field version)"
expect "v0.1.0: display"          "0.1.0"  "$(field display)"
expect "v0.1.0: suite"            "stable" "$(field suite)"
expect "v0.1.0: channel"          "stable" "$(field channel)"
expect "v0.1.0: tag"              "v0.1.0" "$(field tag)"
env_out="$(dv --env)"
expect_re "--env is eval-able"    "^NIMBUS_VERSION=0\.1\.0$" "$(eval "$env_out"; echo "NIMBUS_VERSION=$NIMBUS_VERSION")"

echo "Development build after a release"
commit B
dev_b="$(field version)"
expect_re "after v0.1.0: version" "^0\.1\.0\+git20261117\.1\.${SHA_RE}$" "$dev_b"
expect    "after v0.1.0: channel" "dev" "$(field channel)"

echo "Beta release"
tag v0.2.0-beta.1
expect "v0.2.0-beta.1: version"   "0.2.0~beta.1" "$(field version)"
expect "v0.2.0-beta.1: display"   "0.2.0-beta.1" "$(field display)"
expect "v0.2.0-beta.1: suite"     "beta"         "$(field suite)"
expect "v0.2.0-beta.1: channel"   "beta"         "$(field channel)"

echo "Beta and stable tag on one commit: stable wins"
tag v0.2.0
expect "v0.2.0 + beta tag: version" "0.2.0"  "$(field version)"
expect "v0.2.0 + beta tag: channel" "stable" "$(field channel)"

echo "Base of a development build is the highest ancestor release"
commit C; tag v0.9.0
commit D; tag v0.10.0
commit E
dev_e="$(field version)"
expect_re "base is v0.10.0, not v0.9.0" "^0\.10\.0\+git20261117\.1\.${SHA_RE}$" "$dev_e"

echo "Modified working tree"
printf 'dirty\n' >>"$REPO/f"
expect_re "dirty tree: .dirty suffix" "^0\.10\.0\+git20261117\.1\.${SHA_RE}\.dirty$" "$(field version)"
git -C "$REPO" checkout -q -- f

echo "--tag option"
expect_re "--tag not at HEAD is refused" "does not point at the checked-out commit" "$(dv --tag v0.2.0)"
expect    "--tag unknown locally"        "9.9.9"        "$(field version --tag v9.9.9)"
expect    "--tag beta unknown locally"   "9.9.9~beta.2" "$(field version --tag v9.9.9-beta.2)"
for bad in v1.02.3 v1.2 1.2.3 v1.2.3-rc.1 v1.2.3-beta v1.2.3-beta.01; do
	expect_re "--tag $bad is rejected" "is not a release tag" "$(dv --tag "$bad")"
done
tag v0.11.0-beta.1
printf 'dirty\n' >>"$REPO/f"
expect_re "dirty tree is never labelled a release" "refusing to label it as release" "$(dv --tag v0.11.0-beta.1)"
expect_re "dirty tagged HEAD becomes a dev build"  "^0\.11\.0~beta\.1\+git20261117\.0\.${SHA_RE}\.dirty$" "$(field version)"
git -C "$REPO" checkout -q -- f

echo "Upgrade order (real dpkg --compare-versions)"
expect_order "beta.1 < final"            0.2.0~beta.1  lt 0.2.0
expect_order "beta.9 < beta.10"          0.2.0~beta.9  lt 0.2.0~beta.10
expect_order "last beta < final"         0.2.0~beta.10 lt 0.2.0
expect_order "final < dev build after it" 0.2.0        lt "0.2.0+git20261117.1.gabcdef0"
expect_order "dev build < next beta"     "$dev_e"      lt 0.10.1~beta.1
expect_order "dev after beta < next beta" "0.2.0~beta.1+git20261117.3.gabcdef0" lt 0.2.0~beta.2
expect_order "0.9.0 < 0.10.0"            0.9.0         lt 0.10.0
expect_order "dev build after v0.1.0 > v0.1.0" "$dev_b" gt 0.1.0

echo
printf 'deb-version.test: %d passed, %d failed (dpkg %s)\n' "$passed" "$failed" \
	"$(dpkg-query -W -f='${Version}' dpkg 2>/dev/null || dpkg --version | head -n1)"
((failed == 0))
