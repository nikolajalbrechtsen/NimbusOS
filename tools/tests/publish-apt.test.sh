#!/bin/bash
# =============================================================================
# tools/tests/publish-apt.test.sh
# =============================================================================
# What: runs tools/publish-apt.sh against fake packages (the seven NimbusOS
#       package names, empty) and a throwaway ed25519 key with a passphrase,
#       and checks the repository with a real apt client (file:// source,
#       Signed-By the test key):
#         - init: both suites exist, empty, signed, with Valid-Until 14 days
#           out and Acquire-By-Hash; apt-get update accepts them;
#         - pruning keeps the newest 3 versions per package in dpkg's order,
#           not by name or date: releases published out of order (0.1.0,
#           0.9.0, 0.10.0, then 0.2.0, then 0.10.1) leave 0.10.1, 0.10.0 and
#           0.9.0; in beta, 0.11.0~beta.10 sorts above ~beta.3; and a pool
#           holding 0.2.0~beta.1, 0.2.0~beta.2, 0.2.0, 0.1.10 and 0.1.9
#           keeps 0.2.0, ~beta.2 and ~beta.1 (0.2.0~beta.1 < 0.2.0);
#         - apt-cache madison lists exactly the kept versions, and apt can
#           download an older one (rollback);
#         - by-hash holds the current and the previous indexes only;
#         - the same version again: identical files are accepted, different
#           contents are refused; so are missing notes and packages that
#           don't match the tag;
#         - resign: Date and Valid-Until change, the Packages files, by-hash
#           and the pool stay byte-identical, apt still accepts it;
#         - check finds a changed index;
#         - releases.json: newest first, restart_required from the notes,
#           signature verified with gpgv.
# Why:  docs/PLAN.md NOTE-52 (requested in review: the ~beta ordering).
#
# Usage: tools/tests/publish-apt.test.sh
#        Debian 13 (tools/dev-shell.sh or CI). As root it installs what it
#        needs (apt-utils, dpkg-dev, xz-utils, gpg, gpg-agent, gpgv, python3).
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"
nimbus_load_conf
if [[ "$(id -u)" -eq 0 ]]; then
	nimbus_apt_install_missing apt-utils dpkg-dev xz-utils gpg gpg-agent gpgv python3
fi

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
fails() { ! "$@"; }
nimbus_mktemp_dir T
PUBLISH="$ROOT/tools/publish-apt.sh"

# --- a throwaway signing key with a passphrase -----------------------------------
export GNUPGHOME="$T/gnupg" APT_SIGNING_PASSPHRASE="test passphrase"
install -d -m 0700 "$GNUPGHOME"
gpg --batch --quiet --pinentry-mode loopback --passphrase "$APT_SIGNING_PASSPHRASE" \
	--quick-generate-key "NimbusOS test archive key <test@invalid>" ed25519 sign 1d
KEY="$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
gpg --batch --armor --export "$KEY" >"$T/public.asc"
gpg --batch --dearmor <"$T/public.asc" >"$T/public.gpg"
SITE="$T/site"
REPO="$SITE/apt"
run_publish() { "$PUBLISH" "$@" --site "$SITE" --public-key "$T/public.asc" >>"$T/publish.log" 2>&1; }

# --- fake packages ------------------------------------------------------------------------
make_set() {   # make_set VERSION [EXTRA]: the seven packages at VERSION in $T/debs-VERSION
	local version="$1" extra="${2:-}" dir="$T/debs-$1" pkg
	rm -rf -- "$dir"; mkdir -p "$dir"
	for pkg in "${NIMBUS_PACKAGES[@]}"; do
		mkdir -p "$T/src/$pkg/DEBIAN"
		printf 'Package: %s\nVersion: %s\nArchitecture: all\nMaintainer: tests <tests@invalid>\nDescription: test %s%s\n' \
			"$pkg" "$version" "$pkg" "$extra" >"$T/src/$pkg/DEBIAN/control"
		dpkg-deb --root-owner-group -b "$T/src/$pkg" "$dir/${pkg}_${version}_all.deb" >/dev/null
		rm -rf -- "$T/src/$pkg"
	done
	printf '%s' "$dir"
}
notes() {   # notes TAG [yes]: a release notes file, with Restart-Required: yes if asked
	{ printf 'NimbusOS %s\n\n- Test release.\n' "$1"; [[ -z "${2:-}" ]] || printf 'Restart-Required: yes\n'; } >"$T/notes-$1.md"
	printf '%s' "$T/notes-$1.md"
}
publish() {   # publish TAG VERSION...: build and add releases (pairs)
	while (($#)); do
		run_publish add --key "$KEY" --tag "$1" --debs "$(make_set "$2")" --notes "$(notes "$1")" || return 1
		shift 2
	done
}
pool_versions() {   # pool_versions SUITE PACKAGE: versions in the pool, sorted
	find "$REPO/pool/$1/$2" -name '*.deb' -exec dpkg-deb -f {} Version \; 2>/dev/null | sort | tr '\n' ' '
}
is() { [[ "$1" == "$2" ]]; }
release_field() { sed -n "s/^$2: //p" "$REPO/dists/$1/Release"; }
valid_for_14_days() {
	local d v
	d="$(date -u -d "$(release_field stable Date)" +%s)"
	v="$(date -u -d "$(release_field stable Valid-Until)" +%s)"
	((v - d >= 14 * 86400 - 60 && v - d <= 14 * 86400 + 60))
}
release_fields_set() {
	local r="$REPO/dists/stable/Release"
	grep -qx "Origin: NimbusOS" "$r" && grep -qx "Label: NimbusOS" "$r" && grep -qx "Suite: stable" "$r" \
		&& grep -qx "Acquire-By-Hash: yes" "$r" && grep -qx "Architectures: amd64" "$r"
}
extras_published() { cmp -s "$T/public.asc" "$REPO/nimbusos-archive-keyring.asc" && [[ -f "$SITE/.nojekyll" ]]; }
releases_json() {   # releases_json PYTHON: run PYTHON with r = the releases list
	gpgv --keyring "$T/public.gpg" "$REPO/releases.json.sig" "$REPO/releases.json" \
		&& python3 -c "import json, sys; r = json.load(open(sys.argv[1]))['releases']; $1" "$REPO/releases.json"
}
same_count_everywhere() { local p; for p in "$REPO/pool/stable"/*; do [[ "$(find "$p" -name '*.deb' | wc -l)" == 3 ]] || return 1; done; }

# --- an apt client --------------------------------------------------------------------------
nimbus_apt_private_init "$T/apt"
cat >"$T/apt/etc/sources.list.d/nimbusos.sources" <<EOF
Types: deb
URIs: file://$REPO/
Suites: stable beta
Components: main
Signed-By: $T/public.gpg
EOF
apt_() { APT_CONFIG="$T/apt/apt.conf" "$@"; }
apt_update() { apt_ apt-get update -q -o Acquire::Languages=none; }
madison() { apt_ apt-cache madison "$1" | awk -v s="$2" '$0 ~ s { print $3 }' | sort | tr '\n' ' '; }
rollback_download() { (cd "$T" && apt_ apt-get download -q nimbus-control=0.9.0 && [[ -f nimbus-control_0.9.0_all.deb ]]); }
candidate_is() { local out; out="$(apt_ apt-cache policy "$1")"; grep -q "Candidate: $2\$" <<<"$out"; }   # no pipe: pipefail
BIN="$REPO/dists/stable/main/binary-amd64"
index_hashes() { sha256sum "$BIN/Packages" "$BIN/Packages.xz" | cut -d' ' -f1; }
by_hash_current_and_previous() {   # the current and previous indexes are there; older ones are gone
	local h
	for h in $(index_hashes) $PREVIOUS; do [[ -f "$BIN/by-hash/SHA256/$h" ]] || return 1; done
	for h in $OLDER; do [[ ! -e "$BIN/by-hash/SHA256/$h" ]] || return 1; done
	[[ "$(find "$BIN/by-hash/SHA256" -type f | wc -l)" == 4 && -n "$OLDER" ]]
}

echo "init"
check "init creates an empty, signed repository" run_publish init --key "$KEY"
check "stable and beta exist, each with InRelease, Release.gpg and Packages.xz" \
	test -s "$REPO/dists/stable/InRelease" -a -s "$REPO/dists/beta/Release.gpg" -a -f "$REPO/dists/beta/main/binary-amd64/Packages.xz"
check "Valid-Until is 14 days after Date" valid_for_14_days
check "Origin, Label, Suite, Architectures and Acquire-By-Hash are set" release_fields_set
check "the public key and .nojekyll are published" extras_published
check "apt-get update accepts the empty repository" apt_update
check "releases.json is empty and its signature verifies" releases_json 'assert r == []'

echo "pruning (stable)"
check "publish v0.1.0, v0.9.0 and v0.10.0" publish v0.1.0 0.1.0 v0.9.0 0.9.0 v0.10.0 0.10.0
check "all three are kept" is "$(pool_versions stable nimbus-control)" "0.1.0 0.10.0 0.9.0 "
touch -d 2020-01-01 "$REPO/pool/stable/nimbus-control/"*0.10.0*      # an old file date must not matter
check "publish v0.2.0 after v0.10.0" publish v0.2.0 0.2.0
check "0.1.0 is pruned, not 0.10.0 (newest 3 in dpkg's order: 0.10.0 > 0.9.0 > 0.2.0)" \
	is "$(pool_versions stable nimbus-control)" "0.10.0 0.2.0 0.9.0 "
check "every package keeps the same 3 versions" same_count_everywhere
PREVIOUS="$(index_hashes | tr '\n' ' ')"                                   # current before the next publish
OLDER="$(find "$BIN/by-hash/SHA256" -type f -printf '%f\n' | grep -vxF -f <(index_hashes) | tr '\n' ' ')"
check "publish v0.10.1" publish v0.10.1 0.10.1
check "now 0.10.1, 0.10.0 and 0.9.0 remain" is "$(pool_versions stable nimbus-control)" "0.10.0 0.10.1 0.9.0 "
check "apt-get update accepts it" apt_update
check "apt-cache madison lists exactly the kept versions" is "$(madison nimbus-control stable)" "0.10.0 0.10.1 0.9.0 "
check "an older version can be downloaded (rollback)" rollback_download
check "by-hash holds the current and the previous indexes only" by_hash_current_and_previous

echo "pruning (beta, and ~beta against final versions)"
check "publish v0.11.0-beta.2, -beta.10, -beta.1 and -beta.3" \
	publish v0.11.0-beta.2 0.11.0~beta.2 v0.11.0-beta.10 0.11.0~beta.10 v0.11.0-beta.1 0.11.0~beta.1 v0.11.0-beta.3 0.11.0~beta.3
check "beta keeps ~beta.10, ~beta.3 and ~beta.2 (10 > 3 > 2 > 1)" \
	is "$(pool_versions beta nimbusos-release)" "0.11.0~beta.10 0.11.0~beta.2 0.11.0~beta.3 "
check "stable is untouched by beta releases" is "$(pool_versions stable nimbusos-release)" "0.10.0 0.10.1 0.9.0 "
mkdir -p "$T/mixed"
for v in 0.2.0~beta.1 0.2.0~beta.2 0.2.0 0.1.10 0.1.9; do cp "$(make_set "$v")/nimbus-control_${v}_all.deb" "$T/mixed/"; done
"$PUBLISH" prune --pool "$T/mixed" --keep 3 >>"$T/publish.log" 2>&1 || true
check "prune keeps 0.2.0, ~beta.2 and ~beta.1 of 0.2.0~beta.1, ~beta.2, 0.2.0, 0.1.10, 0.1.9" \
	is "$(for f in "$T/mixed"/*.deb; do dpkg-deb -f "$f" Version; done | sort | tr '\n' ' ')" "0.2.0 0.2.0~beta.1 0.2.0~beta.2 "
check "apt-get update accepts both suites" apt_update
check "the beta suite offers the newest beta" candidate_is nimbus-control "0.11.0~beta.10"

echo "republishing"
check "the same release again, identical files: accepted" \
	run_publish add --key "$KEY" --tag v0.10.1 --debs "$T/debs-0.10.1" --notes "$T/notes-v0.10.1.md"
check "the same version with different contents: refused" \
	fails run_publish add --key "$KEY" --tag v0.10.1 --debs "$(make_set 0.10.1 " (rebuilt)")" --notes "$T/notes-v0.10.1.md"
check "  … saying why" grep -q "already published with different contents" "$T/publish.log"
: >"$T/empty.md"
check "a release without notes: refused" fails run_publish add --key "$KEY" --tag v0.12.0 --debs "$(make_set 0.12.0)" --notes "$T/empty.md"
check "packages that don't match the tag: refused" \
	fails run_publish add --key "$KEY" --tag v0.12.0 --debs "$T/debs-0.10.0" --notes "$T/notes-v0.10.0.md"

echo "re-signing"
sums() { find "$REPO/pool" "$REPO/dists" -type f ! -name Release ! -name InRelease ! -name Release.gpg -print0 | sort -z | xargs -0 sha256sum; }
sums >"$T/before.sha"
date_before="$(release_field stable Date)"
sleep 1
check "resign" run_publish resign --key "$KEY"
sums >"$T/after.sha"
check "Date and Valid-Until changed" fails is "$(release_field stable Date)" "$date_before"
check "Packages, Packages.xz, by-hash and the pool are byte-identical" cmp -s "$T/before.sha" "$T/after.sha"
check "Valid-Until is again 14 days after Date" valid_for_14_days
check "check passes" run_publish check
check "apt-get update accepts the re-signed repository" apt_update

echo "releases.json"
check "newest first, with versions and channels" releases_json '
assert [x["tag"] for x in r][:3] == ["v0.11.0-beta.10", "v0.11.0-beta.3", "v0.11.0-beta.2"], r
assert next(x for x in r if x["tag"] == "v0.10.0")["channel"] == "stable"
assert next(x for x in r if x["tag"] == "v0.9.0")["deb_version"] == "0.9.0"'
run_publish add --key "$KEY" --tag v0.10.2 --debs "$(make_set 0.10.2)" --notes "$(notes v0.10.2 yes)" || true
check "Restart-Required: yes sets restart_required and is left out of the notes" releases_json '
x = next(x for x in r if x["tag"] == "v0.10.2")
assert x["restart_required"] is True and "Restart-Required" not in x["notes"] and "Test release" in x["notes"], x
assert next(x for x in r if x["tag"] == "v0.10.1")["restart_required"] is False'

echo "check"
echo >>"$REPO/dists/beta/main/binary-amd64/Packages"
check "check finds a changed index" fails run_publish check

echo
echo "publish-apt.test: $passed passed, $failed failed"
((failed == 0)) || { echo "--- publish.log (last 30 lines)"; tail -n 30 "$T/publish.log"; exit 1; }
