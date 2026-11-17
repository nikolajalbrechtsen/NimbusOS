#!/bin/bash
# =============================================================================
# tools/fetch-docker-key.sh — fetch, pin and re-check Docker's APT signing key
# =============================================================================
# What: manages build/config/archives/docker.key.chroot, the one copy of
#       Docker's signing key in this repository (nimbusos-docker-keyring is
#       built from it). Three modes:
#
#       --write (default)  download the key from NIMBUS_DOCKER_KEY_URL over
#                          HTTPS and accept it only if it holds exactly one
#                          primary key with the pinned fingerprint
#                          NIMBUS_DOCKER_KEY_FPR:
#                              9DC8 5822 9FC7 DD38 854A  E2D8 8D81 803C 0EBF CD88
#                          Then write it byte for byte as Docker serves it.
#       --check            download it again and compare its key material
#                          with the committed file. Fails if Docker now
#                          serves a different or changed key, so a rotation is
#                          noticed before builds or installed systems break.
#       --verify-local     no network. The committed key file holds exactly
#                          the pinned key; the fingerprint written in the
#                          comments of docker.list.chroot and docker.list.binary
#                          matches it; docker.list.binary names the packaged
#                          keyring (signed-by=NIMBUS_DOCKER_KEYRING_PATH);
#                          docker.list.chroot has no signed-by (live-build
#                          trusts it through the key file); both lists carry
#                          their marker lines and point at NIMBUS_DOCKER_APT_URL.
#
# Why:  the key is committed only after its fingerprint matches, the
#       fingerprint is written in comments, and CI re-checks both
#       (docs/PLAN.md §9, decision 3). The key file itself stays
#       byte-identical to Docker's download: live-build copies it unchanged
#       into the chroot's trusted.gpg.d, and extra text in it would be one
#       more thing for apt to parse.
#
# Usage: tools/fetch-docker-key.sh [--write | --check | --verify-local]
#        Needs curl (except --verify-local) and gpg. Root is not needed.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

key_file=""
archives_dir=""

# download DEST — Docker's key over HTTPS only, with TLS 1.2 or newer.
download() {
	local dest="$1"
	command -v curl >/dev/null 2>&1 || die "curl is required to download the key."
	log "Downloading $NIMBUS_DOCKER_KEY_URL"
	curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
		--max-time 60 --retry 2 --output "$dest" -- "$NIMBUS_DOCKER_KEY_URL" \
		|| die "Could not download $NIMBUS_DOCKER_KEY_URL."
	grep -q -- '-----BEGIN PGP PUBLIC KEY BLOCK-----' "$dest" \
		|| die "$NIMBUS_DOCKER_KEY_URL did not return an ASCII-armored OpenPGP key."
}

# require_pinned FILE WHAT — FILE must hold exactly one primary key, the
# pinned one. Prints the key's user ID and expiry for the log.
require_pinned() {
	local file="$1" what="$2" fprs uid expires
	fprs="$(nimbus_key_fingerprints "$file")"
	if [[ "$fprs" != "$NIMBUS_DOCKER_KEY_FPR" ]]; then
		die "$what does not hold exactly Docker's pinned key.
  expected: $NIMBUS_DOCKER_KEY_FPR
  found:    $(echo "${fprs:-<no key>}" | tr '\n' ' ')
If Docker has really rotated its key, confirm the new fingerprint on
Docker's install documentation, then update NIMBUS_DOCKER_KEY_FPR in
build/nimbus.conf and the fingerprint comments in build/config/archives/."
	fi
	uid="$(GNUPGHOME="$NIMBUS_GNUPGHOME" gpg --batch --quiet --no-autostart --with-colons --show-keys -- "$file" \
		| awk -F: '$1 == "uid" { print $10; exit }')"
	expires="$(GNUPGHOME="$NIMBUS_GNUPGHOME" gpg --batch --quiet --no-autostart --with-colons --show-keys -- "$file" \
		| awk -F: '$1 == "pub" { print $7; exit }')"
	log "$what: $NIMBUS_DOCKER_KEY_FPR ($uid)"
	if [[ -z "$expires" ]]; then
		log "$what: the key does not expire."
	elif ((expires <= $(date -u +%s))); then
		die "$what: the key expired on $(date -u -d "@$expires" +%F)."
	elif ((expires - $(date -u +%s) < 90 * 86400)); then
		warn "$what: the key expires on $(date -u -d "@$expires" +%F), in less than 90 days."
	else
		log "$what: the key expires on $(date -u -d "@$expires" +%F)."
	fi
}

# key_material FILE — the dearmored key packets, so two files can be compared
# independently of armor line wrapping and line endings.
key_material() {
	GNUPGHOME="$NIMBUS_GNUPGHOME" gpg --batch --quiet --no-autostart --dearmor <"$1"
}

mode_write() {
	local tmp
	nimbus_mktemp_dir tmp
	download "$tmp/docker.asc"
	require_pinned "$tmp/docker.asc" "Downloaded key"
	if [[ -f "$key_file" ]] && cmp -s "$tmp/docker.asc" "$key_file"; then
		log "$NIMBUS_DOCKER_KEY_FILE is already up to date."
		return 0
	fi
	install -m 0644 -- "$tmp/docker.asc" "$key_file"
	log "Wrote $NIMBUS_DOCKER_KEY_FILE. Review and commit it."
}

mode_check() {
	local tmp
	nimbus_mktemp_dir tmp
	[[ -s "$key_file" ]] || die "$NIMBUS_DOCKER_KEY_FILE is missing; run tools/fetch-docker-key.sh first."
	download "$tmp/docker.asc"
	require_pinned "$tmp/docker.asc" "Key Docker serves now"
	key_material "$tmp/docker.asc" >"$tmp/served.gpg"
	key_material "$key_file" >"$tmp/committed.gpg"
	if ! cmp -s "$tmp/served.gpg" "$tmp/committed.gpg"; then
		die "Docker now serves different key material for the same fingerprint (new subkeys,
signatures or expiry). Run tools/fetch-docker-key.sh to update
$NIMBUS_DOCKER_KEY_FILE, review the change, and release it through nimbusos-docker-keyring."
	fi
	log "The committed key matches what Docker serves now."
}

# list_fingerprint FILE — the fingerprint written in FILE's comments.
list_fingerprint() {
	grep -E '^#' "$1" | grep -oE '([0-9A-F]{4}[[:space:]]+){9}[0-9A-F]{4}' | head -n 1 | tr -d '[:space:]'
}

mode_verify_local() {
	local list rendered tmp written line marker
	local -a problems=() lines=()
	nimbus_check_docker_key
	require_pinned "$key_file" "$NIMBUS_DOCKER_KEY_FILE"
	nimbus_mktemp_dir tmp

	for list in docker.list.chroot docker.list.binary; do
		[[ -f "$archives_dir/$list" ]] || { problems+=("$list is missing"); continue; }
		marker="$NIMBUS_DOCKER_LIST_CHROOT_MARKER"
		[[ "$list" == *.binary ]] && marker="$NIMBUS_DOCKER_LIST_BINARY_MARKER"
		grep -qxF "# $marker" "$archives_dir/$list" || problems+=("$list lacks its marker line '# $marker'")

		written="$(list_fingerprint "$archives_dir/$list" || true)"
		[[ "$written" == "$NIMBUS_DOCKER_KEY_FPR" ]] \
			|| problems+=("$list states fingerprint '${written:-none}' in its comments, expected $NIMBUS_DOCKER_KEY_FPR")

		# The list exactly as build.sh renders it into the image.
		rendered="$tmp/$list"
		cp -- "$archives_dir/$list" "$rendered"
		nimbus_render_tokens "$rendered"
		mapfile -t lines < <(grep -E '^[[:space:]]*deb[[:space:]]' "$rendered" || true)
		((${#lines[@]})) || problems+=("$list has no deb line")
		for line in "${lines[@]}"; do
			[[ "$line" == *" ${NIMBUS_DOCKER_APT_URL} ${NIMBUS_DEBIAN_SUITE} stable"* ]] \
				|| problems+=("$list: '$line' is not ${NIMBUS_DOCKER_APT_URL} ${NIMBUS_DEBIAN_SUITE} stable")
			if [[ "$list" == *.binary ]]; then
				[[ "$line" == *"signed-by=${NIMBUS_DOCKER_KEYRING_PATH}"[]\ ]* ]] \
					|| problems+=("docker.list.binary: '$line' does not use signed-by=${NIMBUS_DOCKER_KEYRING_PATH}")
			else
				[[ "$line" != *signed-by=* ]] \
					|| problems+=("docker.list.chroot: '$line' must not use signed-by (live-build trusts it through docker.key.chroot)")
			fi
		done
	done

	if ((${#problems[@]})); then
		printf '[%s] ERROR: Docker archive files are inconsistent:\n' "$_nimbus_prog" >&2
		printf '  - %s\n' "${problems[@]}" >&2
		exit 1
	fi
	log "Docker archive files are consistent: key, fingerprint comments, markers and signed-by."
}

main() {
	local mode="write"
	while (($#)); do
		case "$1" in
			--write)        mode="write"; shift ;;
			--check)        mode="check"; shift ;;
			--verify-local) mode="verify-local"; shift ;;
			-h|--help)      usage; exit 0 ;;
			*) die "Unknown option: $1 (see --help)" ;;
		esac
	done
	nimbus_load_conf
	command -v gpg >/dev/null 2>&1 || die "gpg is required."
	key_file="$(nimbus_path "$NIMBUS_DOCKER_KEY_FILE")"
	archives_dir="$(dirname "$key_file")"
	case "$mode" in
		write)        mode_write ;;
		check)        mode_check ;;
		verify-local) mode_verify_local ;;
	esac
}

main "$@"
