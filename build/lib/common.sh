# shellcheck shell=bash
# =============================================================================
# build/lib/common.sh — helpers shared by build/build.sh and tools/*.sh
# =============================================================================
# What: logging and error reporting, nimbus.conf loading and validation,
#       build-host checks, host package installation, OpenPGP key checks,
#       the NimbusOS APT repository check, @NIMBUS_*@ template rendering,
#       the complete `lb config` flag list, and .deb set validation.
#
# Why:  every helper has exactly one implementation. Most importantly, the
#       `lb config` flags live here so build/build.sh and
#       tools/verify-trixie.sh (which proves every flag exists in Trixie's
#       live-build) can never disagree.
#
# Usage (from a script under build/ or tools/):
#       set -Eeuo pipefail
#       # shellcheck source=build/lib/common.sh
#       . "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"
#       nimbus_load_conf
#
#       Scripts must not set their own EXIT trap; use nimbus_mktemp_dir for
#       temporary directories, which are removed automatically on exit.
# =============================================================================

# Source this file only once.
if [[ -n "${_NIMBUS_COMMON_LOADED:-}" ]]; then
	return 0
fi
_NIMBUS_COMMON_LOADED=1

NIMBUS_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
_nimbus_prog="$(basename "$0")"

# The six NimbusOS packages, in build order (docs/PLAN.md §3).
NIMBUS_PACKAGES=(
	nimbusos-archive-keyring
	nimbusos-release
	nimbusos-branding
	nimbus-control
	nimbus-catalog
	nimbusos-desktop
)

# -----------------------------------------------------------------------------
# Logging and error handling
# -----------------------------------------------------------------------------
log()  { printf '[%s] %s\n' "$_nimbus_prog" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$_nimbus_prog" "$*" >&2; }
step() { printf '\n[%s] ==> %s\n' "$_nimbus_prog" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$_nimbus_prog" "$*" >&2; exit 1; }

_nimbus_on_err() {
	printf '[%s] ERROR: "%s" failed with exit status %s (line %s).\n' \
		"$_nimbus_prog" "$3" "$1" "$2" >&2
}
trap '_nimbus_on_err "$?" "$LINENO" "$BASH_COMMAND"' ERR

# -----------------------------------------------------------------------------
# Temporary directories (removed on exit, also after errors)
# -----------------------------------------------------------------------------
_NIMBUS_TMPDIRS=()

_nimbus_cleanup() {
	local d
	for d in "${_NIMBUS_TMPDIRS[@]}"; do
		if [[ -n "$d" && -d "$d" ]]; then
			rm -rf -- "$d"
		fi
	done
}
trap _nimbus_cleanup EXIT

# nimbus_mktemp_dir VARNAME — create a private temp dir and store its path in
# VARNAME. (Not usable inside $(...): the cleanup list lives in this shell.)
nimbus_mktemp_dir() {
	local _dir
	_dir="$(mktemp -d "${TMPDIR:-/tmp}/nimbus.XXXXXXXX")"
	_NIMBUS_TMPDIRS+=("$_dir")
	printf -v "$1" '%s' "$_dir"
}

# -----------------------------------------------------------------------------
# Paths and git
# -----------------------------------------------------------------------------

# nimbus_path PATH — absolute paths pass through; relative ones are taken
# from the repository root (the convention used in nimbus.conf).
nimbus_path() {
	case "$1" in
		/*) printf '%s\n' "$1" ;;
		*)  printf '%s\n' "$NIMBUS_REPO_ROOT/$1" ;;
	esac
}

# nimbus_git ARGS... — git in the repository root. safe.directory avoids
# "dubious ownership" refusals in CI containers, where the checkout belongs to
# a different uid than root.
nimbus_git() {
	git -c safe.directory="$NIMBUS_REPO_ROOT" -C "$NIMBUS_REPO_ROOT" "$@"
}

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
nimbus_load_conf() {
	local conf="${NIMBUS_CONF:-$NIMBUS_REPO_ROOT/build/nimbus.conf}"
	[[ -r "$conf" ]] || die "Cannot read the build configuration: $conf"
	# shellcheck source=build/nimbus.conf
	. "$conf"

	local v
	for v in NIMBUS_NAME NIMBUS_ID NIMBUS_GITHUB_REPO NIMBUS_MAINTAINER \
		NIMBUS_APT_URL NIMBUS_APT_HOST NIMBUS_APT_COMPONENT NIMBUS_APT_ORIGIN \
		NIMBUS_APT_DEFAULT_SUITE NIMBUS_ARCHIVE_KEYS_DIR NIMBUS_ARCHIVE_KEYRING_PATH \
		NIMBUS_DEBIAN_SUITE NIMBUS_ARCH NIMBUS_ARCHIVE_AREAS \
		NIMBUS_MIRROR NIMBUS_MIRROR_SECURITY NIMBUS_BUILD_MIRROR NIMBUS_BUILD_MIRROR_SECURITY \
		NIMBUS_DOCKER_APT_URL NIMBUS_DOCKER_KEY_FPR NIMBUS_DOCKER_KEY_FILE NIMBUS_DOCKER_KEYRING_PATH \
		NIMBUS_HOSTNAME NIMBUS_USER_EXTRA_GROUPS NIMBUS_LIVE_USERNAME NIMBUS_LIVE_USER_GROUPS \
		NIMBUS_IMAGE_NAME NIMBUS_ISO_BASENAME NIMBUS_SQUASHFS_COMPRESSION \
		NIMBUS_MIN_FREE_GIB NIMBUS_WORK_DIR NIMBUS_OUTPUT_DIR NIMBUS_STAGING_DIR NIMBUS_DEBS_DIR; do
		[[ -n "${!v:-}" ]] || die "$v is empty; set it in $conf"
	done

	# These end up on the kernel command line, which separates words by spaces.
	for v in NIMBUS_HOSTNAME NIMBUS_LIVE_USERNAME NIMBUS_LIVE_USER_FULLNAME \
		NIMBUS_LIVE_USER_GROUPS NIMBUS_LOCALE NIMBUS_KEYBOARD_LAYOUTS NIMBUS_TIMEZONE; do
		[[ "${!v}" != *[[:space:]]* ]] || die "$v must not contain spaces (it is a kernel boot parameter): '${!v}'"
	done
	[[ "$NIMBUS_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
		|| die "NIMBUS_HOSTNAME '$NIMBUS_HOSTNAME' is not a valid host name."
	[[ "$NIMBUS_LIVE_USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] \
		|| die "NIMBUS_LIVE_USERNAME '$NIMBUS_LIVE_USERNAME' is not a valid user name."
	[[ "$NIMBUS_MIN_FREE_GIB" =~ ^[0-9]+$ ]] || die "NIMBUS_MIN_FREE_GIB must be a whole number."

	# Fingerprints are compared without spaces, in upper case.
	NIMBUS_DOCKER_KEY_FPR="${NIMBUS_DOCKER_KEY_FPR//[[:space:]]/}"
	NIMBUS_DOCKER_KEY_FPR="${NIMBUS_DOCKER_KEY_FPR^^}"
	[[ "$NIMBUS_DOCKER_KEY_FPR" =~ ^[0-9A-F]{40}$ ]] \
		|| die "NIMBUS_DOCKER_KEY_FPR must be a 40-character fingerprint."

	# One private GnuPG home for every key operation in this run, so nothing
	# touches root's ~/.gnupg. Created here, in the main shell, so it is
	# cleaned up even when used from $(...) subshells.
	nimbus_mktemp_dir NIMBUS_GNUPGHOME
}

# -----------------------------------------------------------------------------
# Build host
# -----------------------------------------------------------------------------
nimbus_require_root() {
	[[ "$(id -u)" -eq 0 ]] || die "This must run as root, for example: sudo $0"
}

# Debian 13 (or a Trixie-based system such as NimbusOS itself) on amd64.
nimbus_check_host() {
	nimbus_require_root
	[[ -r /etc/os-release ]] \
		|| die "Cannot read /etc/os-release; building needs a Debian ${NIMBUS_DEBIAN_VERSION_ID} (${NIMBUS_DEBIAN_SUITE}) host."
	local id id_like codename
	# shellcheck disable=SC1091
	id="$(. /etc/os-release && printf '%s' "${ID:-}")"
	# shellcheck disable=SC1091
	id_like="$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")"
	# shellcheck disable=SC1091
	codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
	if [[ "$id" != debian && " $id_like " != *" debian "* ]]; then
		die "This host is '$id'. Build on Debian ${NIMBUS_DEBIAN_VERSION_ID}, or in a privileged container: docker run --privileged -it -v \"\$PWD\":/src debian:${NIMBUS_DEBIAN_SUITE}"
	fi
	[[ "$codename" == "$NIMBUS_DEBIAN_SUITE" ]] \
		|| die "This host runs '${codename:-unknown}'. Build on Debian ${NIMBUS_DEBIAN_VERSION_ID} (${NIMBUS_DEBIAN_SUITE}), or in a privileged debian:${NIMBUS_DEBIAN_SUITE} container."
	command -v dpkg >/dev/null 2>&1 || die "dpkg not found; this is not a Debian system."
	[[ "$(dpkg --print-architecture)" == "$NIMBUS_ARCH" ]] \
		|| die "This host is $(dpkg --print-architecture); NimbusOS is built on ${NIMBUS_ARCH}."
}

# nimbus_apt_install_missing PKG... — install only what is not installed yet.
_NIMBUS_APT_UPDATED=""
nimbus_apt_install_missing() {
	local p missing=()
	for p in "$@"; do
		if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null || true)" != installed ]]; then
			missing+=("$p")
		fi
	done
	((${#missing[@]})) || return 0
	log "Installing missing host packages: ${missing[*]}"
	if [[ -z "$_NIMBUS_APT_UPDATED" ]]; then
		DEBIAN_FRONTEND=noninteractive apt-get update -q
		_NIMBUS_APT_UPDATED=1
	fi
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}"
}

# nimbus_free_gib DIR — free space in whole GiB on DIR's file system.
nimbus_free_gib() {
	df -P -BG -- "$1" | awk 'NR == 2 { sub(/G$/, "", $4); print $4 }'
}

# -----------------------------------------------------------------------------
# OpenPGP keys
# -----------------------------------------------------------------------------

# nimbus_key_fingerprints FILE — primary-key fingerprints (upper case, one per
# line) of an ASCII-armored or binary key file.
nimbus_key_fingerprints() {
	GNUPGHOME="$NIMBUS_GNUPGHOME" gpg --batch --quiet --no-autostart \
		--with-colons --show-keys -- "$1" 2>/dev/null \
		| awk -F: '$1 == "pub" { want = 1; next }
		           $1 == "fpr" && want { print toupper($10); want = 0 }'
}

# nimbus_dearmor_into DEST FILE... — concatenate the keys of FILE... into one
# binary keyring DEST (the format apt's Signed-By and gpgv expect).
nimbus_dearmor_into() {
	local dest="$1" f
	shift
	: >"$dest"
	for f in "$@"; do
		GNUPGHOME="$NIMBUS_GNUPGHOME" gpg --batch --quiet --no-autostart --dearmor <"$f" >>"$dest"
	done
}

# nimbus_archive_key_files — the committed NimbusOS public keys, one per line.
nimbus_archive_key_files() {
	local dir
	dir="$(nimbus_path "$NIMBUS_ARCHIVE_KEYS_DIR")"
	[[ -d "$dir" ]] || return 0
	find "$dir" -maxdepth 1 -type f -name '*.asc' -print | LC_ALL=C sort
}

# nimbus_archive_fingerprints — fingerprints of all committed keys, sorted.
nimbus_archive_fingerprints() {
	local f
	while IFS= read -r f; do
		nimbus_key_fingerprints "$f"
	done < <(nimbus_archive_key_files) | LC_ALL=C sort -u
}

# nimbus_check_docker_key — the key file live-build and the keyring package
# use must be Docker's key and nothing else.
# Docker fingerprint: 9DC8 5822 9FC7 DD38 854A  E2D8 8D81 803C 0EBF CD88
nimbus_check_docker_key() {
	local file fprs
	file="$(nimbus_path "$NIMBUS_DOCKER_KEY_FILE")"
	[[ -s "$file" ]] || die "Docker's public key is missing: $file. Run tools/fetch-docker-key.sh to download and verify it."
	fprs="$(nimbus_key_fingerprints "$file")"
	[[ "$fprs" == "$NIMBUS_DOCKER_KEY_FPR" ]] || die "$file does not contain exactly Docker's key.
  expected: $NIMBUS_DOCKER_KEY_FPR
  found:    ${fprs:-<no key>}
Run tools/fetch-docker-key.sh to download it again."
	log "Docker key OK ($NIMBUS_DOCKER_KEY_FPR)."
}

# nimbus_check_keyring_deb DEB — the built keyring package must trust exactly
# the committed NimbusOS keys and Docker's key (catches a test-key build).
nimbus_check_keyring_deb() {
	local deb="$1" tmp want got docker
	nimbus_mktemp_dir tmp
	dpkg-deb -x "$deb" "$tmp"
	[[ -s "$tmp$NIMBUS_ARCHIVE_KEYRING_PATH" ]] || die "$(basename "$deb") does not ship $NIMBUS_ARCHIVE_KEYRING_PATH."
	[[ -s "$tmp$NIMBUS_DOCKER_KEYRING_PATH" ]] || die "$(basename "$deb") does not ship $NIMBUS_DOCKER_KEYRING_PATH."
	want="$(nimbus_archive_fingerprints)"
	[[ -n "$want" ]] || die "No NimbusOS public key is committed in $NIMBUS_ARCHIVE_KEYS_DIR/. Create one as described in docs/SIGNING.md."
	got="$(nimbus_key_fingerprints "$tmp$NIMBUS_ARCHIVE_KEYRING_PATH" | LC_ALL=C sort -u)"
	[[ "$got" == "$want" ]] || die "$(basename "$deb") trusts different keys than the ones committed in $NIMBUS_ARCHIVE_KEYS_DIR/.
  in the package: $(echo "$got" | tr '\n' ' ')
  committed:      $(echo "$want" | tr '\n' ' ')
Was it built with tools/build-debs.sh --allow-test-key? Such packages must never go into an image."
	docker="$(nimbus_key_fingerprints "$tmp$NIMBUS_DOCKER_KEYRING_PATH")"
	[[ "$docker" == "$NIMBUS_DOCKER_KEY_FPR" ]] || die "$(basename "$deb") ships a Docker keyring with fingerprint '${docker:-none}', expected $NIMBUS_DOCKER_KEY_FPR."
}

# -----------------------------------------------------------------------------
# NimbusOS APT repository check (docs/PLAN.md NOTE-04)
# -----------------------------------------------------------------------------
# nimbus_check_repo [SUITE] — InRelease must be reachable, signed by a
# committed key, and not expired. Any failure stops the build with a message
# that says how to fix it.
nimbus_check_repo() {
	local suite="${1:-$NIMBUS_APT_DEFAULT_SUITE}" tmp url code detail valid_until valid_until_epoch
	local -a keys
	nimbus_mktemp_dir tmp
	url="${NIMBUS_APT_URL}/dists/${suite}/InRelease"
	log "Checking the NimbusOS APT repository: $url"

	code="$(curl --silent --show-error --location --max-time 30 --retry 2 \
		--output "$tmp/InRelease" --write-out '%{http_code}' "$url" 2>"$tmp/curl.err")" || code="none"
	if [[ "$code" != 200 ]]; then
		if [[ "$code" == none ]]; then
			detail="$(tr '\n' ' ' <"$tmp/curl.err")"
		else
			detail="HTTP status $code"
		fi
		die "The NimbusOS APT repository does not answer: $detail
  URL: $url
The image's APT configuration points at this repository, and live-build
refreshes the package indices at the end of the chroot stage, so the build
would fail there after a long wait.
- If the repository has never been published: run the refresh workflow once
  (GitHub -> Actions -> refresh -> Run workflow), wait for it to finish, then
  build again.
- If you are offline or behind a proxy: fix network access first.
Use --skip-repo-check only if you are sure the repository will be reachable
while the image is built."
	fi

	mapfile -t keys < <(nimbus_archive_key_files)
	((${#keys[@]})) || die "No NimbusOS public key is committed in $NIMBUS_ARCHIVE_KEYS_DIR/. Create one as described in docs/SIGNING.md."
	nimbus_dearmor_into "$tmp/archive.gpg" "${keys[@]}"
	if ! gpgv --keyring "$tmp/archive.gpg" "$tmp/InRelease" >"$tmp/gpgv.out" 2>&1; then
		die "The repository's InRelease is not signed by a committed NimbusOS key.
$(sed 's/^/  gpgv: /' "$tmp/gpgv.out")
Check that APT_SIGNING_KEY matches a key in $NIMBUS_ARCHIVE_KEYS_DIR/ (docs/SIGNING.md)."
	fi

	valid_until="$(awk '/^Valid-Until: / { sub(/^Valid-Until: /, ""); print; exit }' "$tmp/InRelease")"
	if [[ -n "$valid_until" ]]; then
		valid_until_epoch="$(date -u -d "$valid_until" +%s)" || die "Cannot read Valid-Until '$valid_until' in $url."
		if ((valid_until_epoch <= $(date -u +%s))); then
			die "The repository's metadata expired on $valid_until.
Run the refresh workflow (GitHub -> Actions -> refresh -> Run workflow) to re-sign it, then build again."
		fi
	else
		warn "$url has no Valid-Until field; expected one from reprepro's ValidFor."
	fi
	log "Repository OK: signed by a committed key, valid until ${valid_until:-<not set>}."
}

# -----------------------------------------------------------------------------
# @NIMBUS_*@ templates (docs/PLAN.md NOTE-49)
# -----------------------------------------------------------------------------

# nimbus_render_tokens FILE — replace every @NIMBUS_X@ in FILE (in place) with
# the value of NIMBUS_X. Any other @TOKEN@ (for example live-build's own
# @APPEND_LIVE@) is left untouched. An unknown NIMBUS token is an error.
nimbus_render_tokens() {
	local file="$1" content var token
	IFS= read -r -d '' content <"$file" || true
	for var in $(compgen -v NIMBUS_); do
		token="@${var}@"
		[[ "$content" == *"$token"* ]] || continue
		content="${content//"$token"/"${!var}"}"
	done
	if [[ "$content" =~ @NIMBUS_[A-Z0-9_]+@ ]]; then
		die "$file uses ${BASH_REMATCH[0]}, which build/nimbus.conf does not define."
	fi
	printf '%s' "$content" >"$file"
}

# nimbus_render_tree DIR — render every text file under DIR that has tokens.
nimbus_render_tree() {
	local f
	local -a files
	mapfile -d '' -t files < <(grep -rlIZ -E '@NIMBUS_[A-Z0-9_]+@' -- "$1" || true)
	for f in "${files[@]}"; do
		nimbus_render_tokens "$f"
	done
}

# -----------------------------------------------------------------------------
# live-build configuration — the single source of truth for `lb config`
# -----------------------------------------------------------------------------
# nimbus_lb_config_args DISPLAY_VERSION — fills the array NIMBUS_LB_ARGS.
# Debian-mode defaults that are not listed (init system: systemd; live-build's
# own apt/debootstrap choices) are relied upon deliberately.
nimbus_lb_config_args() {
	local display_version="$1" volume append
	volume="${NIMBUS_ISO_VOLUME_PREFIX} ${display_version}"
	volume="${volume:0:32}"                     # ISO 9660 volume IDs are at most 32 characters

	# Kernel command line of the live session (read by live-boot/live-config).
	append="boot=live components ${NIMBUS_BOOT_EXTRA}"
	append+=" hostname=${NIMBUS_HOSTNAME}"
	append+=" username=${NIMBUS_LIVE_USERNAME}"
	append+=" user-fullname=${NIMBUS_LIVE_USER_FULLNAME}"
	append+=" user-default-groups=${NIMBUS_LIVE_USER_GROUPS}"   # docs/PLAN.md §2 (users and groups)
	append+=" locales=${NIMBUS_LOCALE}"
	append+=" keyboard-layouts=${NIMBUS_KEYBOARD_LAYOUTS}"
	append+=" timezone=${NIMBUS_TIMEZONE}"

	NIMBUS_LB_ARGS=(
		# Plain Debian (not a derivative of Ubuntu etc.), building a live system.
		--mode debian
		--system live
		--distribution "$NIMBUS_DEBIAN_SUITE"
		--architecture "$NIMBUS_ARCH"
		# All four Trixie components: firmware lives in non-free-firmware.
		--archive-areas "$NIMBUS_ARCHIVE_AREAS"

		# Mirrors used during the build (may be a local cache) ...
		--mirror-bootstrap "$NIMBUS_BUILD_MIRROR"
		--mirror-chroot "$NIMBUS_BUILD_MIRROR"
		--mirror-chroot-security "$NIMBUS_BUILD_MIRROR_SECURITY"
		# ... and the public mirrors written into the image.
		--mirror-binary "$NIMBUS_MIRROR"
		--mirror-binary-security "$NIMBUS_MIRROR_SECURITY"
		# Include trixie-security and trixie-updates; no backports.
		--security true
		--updates true
		--backports false

		# Recommends on: a Plasma desktop relies on them.
		--apt-recommends true
		# Keep package indices in the image so apt works in the live session.
		--apt-indices true
		# No deb-src lines in the image.
		--apt-source-archives false
		# Non-interactive, with retries for flaky mirrors.
		--apt-options "--yes -o Acquire::Retries=5"
		--debconf-frontend noninteractive

		# live-boot initramfs; the Debian kernel flavour for amd64.
		--initramfs live-boot
		--linux-flavours "$NIMBUS_ARCH"
		# Only the firmware packages in our package lists (10-base) instead of
		# every firmware package in the archive, which keeps the ISO smaller.
		--firmware-chroot false
		--firmware-binary false
		# Calamares is the installer; no debian-installer on the ISO.
		--debian-installer none

		# One hybrid ISO that boots from DVD or USB on UEFI (GRUB) and legacy
		# BIOS (ISOLINUX). Secure Boot uses Debian's signed shim and GRUB.
		--binary-images iso-hybrid
		--bootloaders "grub-efi,syslinux"
		--uefi-secure-boot "$NIMBUS_UEFI_SECURE_BOOT"
		--bootappend-live "$append"
		--memtest none
		# xz gives the smallest squashfs (docs/PLAN.md NOTE-41).
		--chroot-squashfs-compression-type "$NIMBUS_SQUASHFS_COMPRESSION"

		# Output naming and ISO metadata.
		--image-name "$NIMBUS_IMAGE_NAME"
		--iso-application "$NIMBUS_ISO_APPLICATION"
		--iso-publisher "$NIMBUS_ISO_PUBLISHER"
		--iso-volume "$volume"
		# sha256sum.txt inside the ISO for the boot menu's integrity check.
		--checksums sha256
		--zsync false
		# No source image.
		--source false

		# Reuse downloaded packages between builds; build/build.sh still
		# re-bootstraps every time.
		--cache true
		--cache-packages true
		--cache-indices false
	)
}

# -----------------------------------------------------------------------------
# .deb sets
# -----------------------------------------------------------------------------

# nimbus_check_deb_set DIR — DIR must contain exactly one .deb of each of the
# six packages, all with the same version. Prints that version.
nimbus_check_deb_set() {
	local dir="$1" pkg v version=""
	local -a found
	for pkg in "${NIMBUS_PACKAGES[@]}"; do
		mapfile -t found < <(find "$dir" -maxdepth 1 -type f -name "${pkg}_*.deb" | LC_ALL=C sort)
		((${#found[@]} == 1)) \
			|| die "Expected exactly one ${pkg}_*.deb in $dir, found ${#found[@]}. Use an empty directory per build."
		[[ "$(dpkg-deb -f "${found[0]}" Package)" == "$pkg" ]] \
			|| die "$(basename "${found[0]}") is not the package $pkg."
		v="$(dpkg-deb -f "${found[0]}" Version)"
		if [[ -z "$version" ]]; then
			version="$v"
		elif [[ "$v" != "$version" ]]; then
			die "Version mismatch in $dir: $(basename "${found[0]}") is $v, others are $version."
		fi
	done
	printf '%s\n' "$version"
}
