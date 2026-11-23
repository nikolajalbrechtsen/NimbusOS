#!/bin/bash
# =============================================================================
# tools/publish-apt.sh: build, prune and sign the NimbusOS APT repository
# =============================================================================
# What: maintains the tree GitHub Pages serves from the gh-pages branch:
#         SITE/.nojekyll                        Pages serves the files as they are
#         SITE/apt/                             = NIMBUS_APT_URL
#           pool/<suite>/<package>/*.deb        the published packages
#           dists/<suite>/main/binary-amd64/    Packages, Packages.xz, by-hash/
#           dists/<suite>/{Release,InRelease,Release.gpg}
#           releases.json, releases.json.sig    release notes (docs/PLAN.md NOTE-37, NOTE-38)
#           nimbusos-archive-keyring.asc        the public key(s), for checking downloads
#       with apt-ftparchive (apt-utils), which needs no database between runs
#       (docs/PLAN.md NOTE-52). Commands:
#         init    create every suite in NIMBUS_APT_SUITES, empty, signed; and
#                 an empty releases.json (the first setup, NOTE-04)
#         add     publish the seven .debs of release TAG into its suite
#                 (stable, or beta for -beta tags), keep the newest
#                 NIMBUS_APT_KEEP_VERSIONS versions of each package in that
#                 suite (dpkg --compare-versions order, never names or dates),
#                 rebuild that suite's indexes and sign them; add TAG to
#                 releases.json with its notes file and sign that too
#         resign  only re-sign: rewrite Date and Valid-Until in each existing
#                 Release and sign it again. Packages, pool and by-hash files
#                 are not touched (refresh.yml, weekly)
#         check   what a client checks: InRelease and Release.gpg verify
#                 against the public keys, Valid-Until is in the future, every
#                 index matches its Release checksum, and every package an
#                 index lists is in the pool with that checksum
#         prune   prune one pool directory (used by add; exposed for the test)
#       Release files carry Origin and Label NIMBUS_APT_ORIGIN, Valid-Until
#       NIMBUS_APT_VALID_DAYS after signing, and Acquire-By-Hash: index files
#       are also kept under by-hash/ (the current and the previous ones), so a
#       client updating while a publish replaces the indexes still gets
#       matching files. Signatures use the key given with --key, from
#       GNUPGHOME, with the passphrase (if any) in APT_SIGNING_PASSPHRASE.
# Why:  spec 4.2: suites stable and beta, signed with Valid-Until 14 days
#       out, the last 3 versions of each package kept for rollback
#       (`apt install <pkg>=<version>`), releases.json next to the repository.
#
# Usage: tools/publish-apt.sh init   --site DIR --key FPR [--public-key FILE]
#        tools/publish-apt.sh add    --site DIR --key FPR --tag TAG --debs DIR --notes FILE [--public-key FILE]
#        tools/publish-apt.sh resign --site DIR --key FPR [--public-key FILE]
#        tools/publish-apt.sh check  --site DIR [--public-key FILE]
#        tools/publish-apt.sh prune  --pool DIR --keep N
#   --public-key FILE  the public key(s) clients trust; default: the committed
#                      keys in NIMBUS_ARCHIVE_KEYS_DIR. --key must be one of them.
#   Needs apt-utils, dpkg-dev, xz-utils, gpg, gpgv and python3 (Debian 13).
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# --- options ----------------------------------------------------------------------
cmd="${1:-}"
[[ -n "$cmd" && "$cmd" != -* ]] || { usage >&2; exit 2; }
shift
site="" signing_key="" tag="" debs="" notes="" public_key="" pool="" keep=""
while (($#)); do
	case "$1" in
		--site) site="${2:?--site needs a directory}"; shift 2 ;;
		--key) signing_key="${2:?--key needs a fingerprint}"; shift 2 ;;
		--tag) tag="${2:?--tag needs a tag}"; shift 2 ;;
		--debs) debs="${2:?--debs needs a directory}"; shift 2 ;;
		--notes) notes="${2:?--notes needs a file}"; shift 2 ;;
		--public-key) public_key="${2:?--public-key needs a file}"; shift 2 ;;
		--pool) pool="${2:?--pool needs a directory}"; shift 2 ;;
		--keep) keep="${2:?--keep needs a number}"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) die "Unknown option: $1 (see --help)" ;;
	esac
done

nimbus_load_conf
ARCH=amd64                       # every NimbusOS package is Architecture: all; clients ask for amd64
COMPONENT="$NIMBUS_APT_COMPONENT"

# --- pruning ----------------------------------------------------------------------
# newest_first VERSION... : the versions sorted newest first by dpkg's rules.
newest_first() {
	local -a sorted=()
	local v i j
	for v in "$@"; do
		sorted+=("$v")
		i=$((${#sorted[@]} - 1))
		while ((i > 0)) && dpkg --compare-versions "${sorted[i]}" gt "${sorted[i - 1]}"; do
			j="${sorted[i - 1]}"; sorted[i - 1]="${sorted[i]}"; sorted[i]="$j"
			i=$((i - 1))
		done
	done
	((${#sorted[@]})) && printf '%s\n' "${sorted[@]}"
	return 0
}

# prune_pool DIR KEEP: for each package in DIR, keep the KEEP newest versions.
prune_pool() {
	local dir="$1" n="$2" f pkg version
	local -A by_package=() file_of=()
	[[ "$n" =~ ^[1-9][0-9]*$ ]] || die "--keep must be a whole number of at least 1."
	[[ -d "$dir" ]] || return 0
	while IFS= read -r -d '' f; do
		pkg="$(dpkg-deb -f "$f" Package)"
		version="$(dpkg-deb -f "$f" Version)"
		[[ -z "${file_of[$pkg/$version]:-}" ]] || die "Two files in $dir hold $pkg $version: ${file_of[$pkg/$version]} and $f."
		file_of[$pkg/$version]="$f"
		by_package[$pkg]+="$version "
	done < <(find "$dir" -type f -name '*.deb' -print0)
	for pkg in "${!by_package[@]}"; do
		local -a versions
		# shellcheck disable=SC2086  # the list is space-separated versions
		mapfile -t versions < <(newest_first ${by_package[$pkg]})
		for version in "${versions[@]:n}"; do
			log "Pruning $pkg $version (keeping the newest $n)"
			rm -f -- "${file_of[$pkg/$version]}"
		done
	done
	find "$dir" -mindepth 1 -type d -empty -delete
}

if [[ "$cmd" == prune ]]; then
	[[ -n "$pool" && -n "$keep" ]] || die "prune needs --pool and --keep."
	prune_pool "$pool" "$keep"
	exit 0
fi

# --- keys and signing ---------------------------------------------------------------
[[ -n "$site" ]] || die "$cmd needs --site (the gh-pages tree)."
REPO="$site/apt"
nimbus_mktemp_dir WORK
if [[ -n "$public_key" ]]; then
	public_keys=("$public_key")
else
	mapfile -t public_keys < <(nimbus_archive_key_files)
	((${#public_keys[@]})) || die "No NimbusOS public key is committed in $NIMBUS_ARCHIVE_KEYS_DIR/ (docs/SIGNING.md)."
fi
nimbus_dearmor_into "$WORK/trusted.gpg" "${public_keys[@]}"
mapfile -t trusted_fprs < <(for f in "${public_keys[@]}"; do nimbus_key_fingerprints "$f"; done | LC_ALL=C sort -u)

check_signing_key() {
	[[ -n "$signing_key" ]] || die "$cmd needs --key (the signing key's fingerprint in GNUPGHOME)."
	signing_key="${signing_key^^}"
	local fpr is_trusted=0
	for fpr in "${trusted_fprs[@]}"; do [[ "$fpr" == "$signing_key" ]] && is_trusted=1; done
	((is_trusted)) || die "The signing key $signing_key is not one of the public keys clients trust (${trusted_fprs[*]}).
Sign with the key whose public half is committed in $NIMBUS_ARCHIVE_KEYS_DIR/ (docs/SIGNING.md)."
	gpg --batch --list-secret-keys "$signing_key" >/dev/null 2>&1 \
		|| die "The secret key $signing_key is not in GNUPGHOME (${GNUPGHOME:-~/.gnupg})."
}

sign() {   # sign MODE IN OUT: MODE clearsign | detach (armored) | detach-binary
	local -a args=(--batch --yes --pinentry-mode loopback --passphrase-fd 0 --local-user "$signing_key" --digest-algo SHA512)
	case "$1" in
		clearsign) args+=(--clearsign) ;;
		detach) args+=(--armor --detach-sign) ;;
		detach-binary) args+=(--detach-sign) ;;
	esac
	printf '%s' "${APT_SIGNING_PASSPHRASE:-}" | gpg "${args[@]}" --output "$3" "$2" 2>"$WORK/gpg.err" \
		|| die "Signing $2 failed: $(tr '\n' ' ' <"$WORK/gpg.err")"
}

sign_release() {   # sign_release SUITE_DIR: InRelease and Release.gpg from Release
	local d="$1"
	sign clearsign "$d/Release" "$d/InRelease.new"
	sign detach "$d/Release" "$d/Release.gpg.new"
	mv -f -- "$d/InRelease.new" "$d/InRelease"
	mv -f -- "$d/Release.gpg.new" "$d/Release.gpg"
	gpgv --keyring "$WORK/trusted.gpg" "$d/InRelease" >/dev/null 2>&1 \
		|| die "$d/InRelease doesn't verify against the trusted keys."
}

# --- indexes ---------------------------------------------------------------------------
release_hashes() {   # release_hashes RELEASE: the SHA256 and SHA512 sums it lists
	[[ -f "$1" ]] || return 0
	awk '/^(SHA256|SHA512):/ { in_sums = 1; next } /^[^ ]/ { in_sums = 0 } in_sums { print $1 }' "$1"
}

index_suite() {   # index_suite SUITE: Packages, Packages.xz, Release, by-hash, signatures
	local suite="$1" d bin old_release h f
	d="$REPO/dists/$suite"
	bin="$d/$COMPONENT/binary-$ARCH"
	mkdir -p -- "$bin" "$REPO/pool/$suite"
	old_release="$WORK/Release.$suite.old"
	if [[ -f "$d/Release" ]]; then cp -- "$d/Release" "$old_release"; else : >"$old_release"; fi
	(cd "$REPO" && apt-ftparchive packages "pool/$suite") >"$bin/Packages.new"
	mv -f -- "$bin/Packages.new" "$bin/Packages"
	xz -9 --keep --force -- "$bin/Packages"
	rm -f -- "$d/Release" "$d/InRelease" "$d/Release.gpg"
	(cd "$REPO" && apt-ftparchive \
		-o "APT::FTPArchive::Release::Origin=$NIMBUS_APT_ORIGIN" \
		-o "APT::FTPArchive::Release::Label=$NIMBUS_APT_ORIGIN" \
		-o "APT::FTPArchive::Release::Suite=$suite" \
		-o "APT::FTPArchive::Release::Codename=$suite" \
		-o "APT::FTPArchive::Release::Architectures=$ARCH" \
		-o "APT::FTPArchive::Release::Components=$COMPONENT" \
		-o "APT::FTPArchive::Release::Description=$NIMBUS_NAME packages ($suite)" \
		-o "APT::FTPArchive::Release::ValidTime=$((NIMBUS_APT_VALID_DAYS * 86400))" \
		-o "APT::FTPArchive::DoByHash=true" \
		release "dists/$suite") >"$WORK/Release"
	mv -f -- "$WORK/Release" "$d/Release"
	# by-hash: keep the files the new and the previous Release name.
	local -A wanted=()
	while IFS= read -r h; do wanted[$h]=1; done < <(release_hashes "$d/Release"; release_hashes "$old_release")
	while IFS= read -r -d '' f; do
		[[ -n "${wanted[$(basename "$f")]:-}" ]] || rm -f -- "$f"
	done < <(find "$bin/by-hash" -type f -print0 2>/dev/null)
	sign_release "$d"
	log "Indexed $suite: $(grep -c '^Package: ' "$bin/Packages" || true) package versions, valid until $(sed -n 's/^Valid-Until: //p' "$d/Release")."
}

write_extras() {   # .nojekyll and the public key file
	: >"$site/.nojekyll"
	cat -- "${public_keys[@]}" >"$REPO/nimbusos-archive-keyring.asc"
}

sign_releases_json() {
	sign detach-binary "$REPO/releases.json" "$REPO/releases.json.sig"
	gpgv --keyring "$WORK/trusted.gpg" "$REPO/releases.json.sig" "$REPO/releases.json" >/dev/null 2>&1 \
		|| die "releases.json.sig doesn't verify against the trusted keys."
}

# --- commands ----------------------------------------------------------------------------
do_init() {
	check_signing_key
	local suite
	mkdir -p -- "$REPO"
	for suite in $NIMBUS_APT_SUITES; do
		if [[ -f "$REPO/dists/$suite/Release" ]]; then
			log "Suite $suite exists already; leaving it as it is."
		else
			index_suite "$suite"
		fi
	done
	if [[ ! -f "$REPO/releases.json" ]]; then
		python3 "$NIMBUS_REPO_ROOT/tools/releases-json.py" empty --out "$REPO/releases.json"
		sign_releases_json
	fi
	write_extras
}

do_add() {
	check_signing_key
	[[ -n "$tag" && -n "$debs" && -n "$notes" ]] || die "add needs --tag, --debs and --notes."
	[[ -s "$notes" ]] || die "The release notes $notes are missing or empty (docs/PLAN.md NOTE-37)."
	local suite channel version expected f pkg dest
	# The same tag rules as tools/deb-version.sh (build/lib/common.sh), without
	# its checks of the working tree: publishing only needs the mapping.
	expected="$(nimbus_tag_to_debian "$tag")" || die "'$tag' is not a release tag (vX.Y.Z or vX.Y.Z-beta.N)."
	channel="$(nimbus_tag_channel "$tag")"
	suite="$channel"
	[[ " $NIMBUS_APT_SUITES " == *" $suite "* ]] || die "$tag maps to suite '$suite', which isn't published (NIMBUS_APT_SUITES: $NIMBUS_APT_SUITES)."
	version="$(nimbus_check_deb_set "$debs")"
	[[ "$version" == "$expected" ]] || die "The packages in $debs are version $version, but $tag is $expected."
	step "Publishing $NIMBUS_NAME ${tag#v} ($version) to $suite"

	local suite_name
	for suite_name in $NIMBUS_APT_SUITES; do
		[[ -f "$REPO/dists/$suite_name/Release" || "$suite_name" == "$suite" ]] || index_suite "$suite_name"
	done
	for pkg in "${NIMBUS_PACKAGES[@]}"; do
		f="$(find "$debs" -maxdepth 1 -type f -name "${pkg}_*.deb" -print -quit)"
		dest="$REPO/pool/$suite/$pkg/$(basename "$f")"
		if [[ -f "$dest" ]]; then
			cmp -s -- "$f" "$dest" || die "$pkg $version is already published with different contents.
A published version must never change: tag a new version instead."
			log "$pkg $version is already published (identical); kept."
		else
			install -D -m 0644 -- "$f" "$dest"
		fi
	done
	prune_pool "$REPO/pool/$suite" "$NIMBUS_APT_KEEP_VERSIONS"
	index_suite "$suite"

	local current="$REPO/releases.json"
	[[ -f "$current" ]] || python3 "$NIMBUS_REPO_ROOT/tools/releases-json.py" empty --out "$current"
	python3 "$NIMBUS_REPO_ROOT/tools/releases-json.py" add --current "$current" --out "$WORK/releases.json" \
		--tag "$tag" --version "${tag#v}" --deb-version "$version" --channel "$channel" \
		--notes "$notes" --url "$NIMBUS_HOME_URL/releases/tag/$tag"
	mv -f -- "$WORK/releases.json" "$current"
	sign_releases_json
	write_extras
}

do_resign() {
	check_signing_key
	local suite d now until resigned=0
	now="$(LC_ALL=C date -u -R)"
	until="$(LC_ALL=C date -u -R -d "+$NIMBUS_APT_VALID_DAYS days")"
	for suite in $NIMBUS_APT_SUITES; do
		d="$REPO/dists/$suite"
		[[ -f "$d/Release" ]] || { warn "No Release for suite $suite; run init or add first."; continue; }
		resigned=1
		awk -v now="$now" -v until="$until" '
			/^Date: / { print "Date: " now; next }
			/^Valid-Until: / { print "Valid-Until: " until; seen = 1; next }
			/^(MD5Sum|SHA1|SHA256|SHA512):/ && !seen { print "Valid-Until: " until; seen = 1 }
			{ print }' "$d/Release" >"$WORK/Release"
		mv -f -- "$WORK/Release" "$d/Release"
		sign_release "$d"
		log "Re-signed $suite: valid until $until."
	done
	((resigned)) || die "Nothing to re-sign in $REPO. Create the repository first: tools/publish-apt.sh init."
}

do_check() {
	local suite d bin issues=0 valid_until name sum file
	for suite in $NIMBUS_APT_SUITES; do
		d="$REPO/dists/$suite"
		if [[ ! -f "$d/InRelease" ]]; then
			warn "$suite: no InRelease"; issues=$((issues + 1)); continue
		fi
		gpgv --keyring "$WORK/trusted.gpg" "$d/InRelease" >/dev/null 2>&1 \
			|| { warn "$suite: InRelease doesn't verify"; issues=$((issues + 1)); }
		gpgv --keyring "$WORK/trusted.gpg" "$d/Release.gpg" "$d/Release" >/dev/null 2>&1 \
			|| { warn "$suite: Release.gpg doesn't verify"; issues=$((issues + 1)); }
		valid_until="$(sed -n 's/^Valid-Until: //p' "$d/Release")"
		if [[ -z "$valid_until" ]] || (($(date -u -d "$valid_until" +%s) <= $(date -u +%s))); then
			warn "$suite: Valid-Until '${valid_until:-missing}' is not in the future"; issues=$((issues + 1))
		fi
		while read -r sum _ name; do
			[[ -f "$d/$name" ]] || continue
			[[ "$(sha256sum <"$d/$name" | cut -d' ' -f1)" == "$sum" ]] \
				|| { warn "$suite: $name doesn't match its Release checksum"; issues=$((issues + 1)); }
		done < <(awk '/^SHA256:/ { s = 1; next } /^[^ ]/ { s = 0 } s { print $1, $2, $3 }' "$d/Release")
		bin="$d/$COMPONENT/binary-$ARCH"
		while read -r file sum; do
			[[ -f "$REPO/$file" && "$(sha256sum <"$REPO/$file" | cut -d' ' -f1)" == "$sum" ]] \
				|| { warn "$suite: $file is missing or differs from Packages"; issues=$((issues + 1)); }
		done < <(awk '/^Filename: / { f = $2 } /^SHA256: / { print f, $2 }' "$bin/Packages")
		log "$suite: $(grep -c '^Package: ' "$bin/Packages" || true) package versions, valid until $valid_until."
	done
	if [[ -f "$REPO/releases.json" ]]; then
		gpgv --keyring "$WORK/trusted.gpg" "$REPO/releases.json.sig" "$REPO/releases.json" >/dev/null 2>&1 \
			|| { warn "releases.json.sig doesn't verify"; issues=$((issues + 1)); }
	else
		warn "no releases.json"; issues=$((issues + 1))
	fi
	((issues == 0)) || die "$issues problem(s) in $REPO."
	log "Repository OK: $REPO"
}

case "$cmd" in
	init) do_init ;;
	add) do_add ;;
	resign) do_resign ;;
	check) do_check ;;
	*) die "Unknown command: $cmd (init, add, resign, check or prune)" ;;
esac
