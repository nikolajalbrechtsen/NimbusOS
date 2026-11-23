# shellcheck shell=bash
# =============================================================================
# build/lib/common.sh — helpers shared by build/build.sh and tools/*.sh
# =============================================================================
# What: logging and error reporting, nimbus.conf loading and validation,
#       build-host checks, host package installation, OpenPGP key checks,
#       the NimbusOS APT repository check, private APT configurations,
#       live-build package-list parsing, @NIMBUS_*@ template rendering,
#       the complete `lb config` flag list, .deb set validation and the
#       final image's APT check.
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

# The seven NimbusOS packages, in build order (docs/PLAN.md §3).
NIMBUS_PACKAGES=(
	nimbusos-archive-keyring
	nimbusos-docker-keyring
	nimbusos-release
	nimbusos-branding
	nimbus-control
	nimbus-catalog
	nimbusos-desktop
)

# Marker comments in build/config/archives/docker.list.{chroot,binary}. The
# image check below uses them to prove the final image carries the .binary
# source and not the build-time one (docs/PLAN.md NOTE-03).
NIMBUS_DOCKER_LIST_CHROOT_MARKER="nimbus:docker.list.chroot"
NIMBUS_DOCKER_LIST_BINARY_MARKER="nimbus:docker.list.binary"

# Release tags: vMAJOR.MINOR.PATCH with an optional -beta.N, no leading zeros
# (docs/PLAN.md NOTE-32). tools/deb-version.sh and tools/publish-apt.sh use these.
NIMBUS_TAG_RE='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-beta\.(0|[1-9][0-9]*))?$'

# nimbus_tag_to_debian TAG — the Debian version of a release tag (v0.2.1-beta.3
# -> 0.2.1~beta.3, so a final release sorts above its betas), or fail.
nimbus_tag_to_debian() {
	[[ "$1" =~ $NIMBUS_TAG_RE ]] || return 1
	local v="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
	if [[ -n "${BASH_REMATCH[5]}" ]]; then
		v+="~beta.${BASH_REMATCH[5]}"
	fi
	printf '%s\n' "$v"
}

# nimbus_tag_channel TAG — beta for -beta.N tags, else stable (also the APT suite).
nimbus_tag_channel() {
	if [[ "$1" == *-beta.* ]]; then printf 'beta\n'; else printf 'stable\n'; fi
}

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

# nimbus_check_archive_keyring_deb DEB — nimbusos-archive-keyring must trust
# exactly the committed NimbusOS keys (catches a test-key build).
nimbus_check_archive_keyring_deb() {
	local deb="$1" tmp want got
	nimbus_mktemp_dir tmp
	dpkg-deb -x "$deb" "$tmp"
	[[ -s "$tmp$NIMBUS_ARCHIVE_KEYRING_PATH" ]] || die "$(basename "$deb") does not ship $NIMBUS_ARCHIVE_KEYRING_PATH."
	want="$(nimbus_archive_fingerprints)"
	[[ -n "$want" ]] || die "No NimbusOS public key is committed in $NIMBUS_ARCHIVE_KEYS_DIR/. Create one as described in docs/SIGNING.md."
	got="$(nimbus_key_fingerprints "$tmp$NIMBUS_ARCHIVE_KEYRING_PATH" | LC_ALL=C sort -u)"
	[[ "$got" == "$want" ]] || die "$(basename "$deb") trusts different keys than the ones committed in $NIMBUS_ARCHIVE_KEYS_DIR/.
  in the package: $(echo "$got" | tr '\n' ' ')
  committed:      $(echo "$want" | tr '\n' ' ')
Was it built with tools/build-debs.sh --allow-test-key? Such packages must never go into an image."
}

# nimbus_check_docker_keyring_deb DEB — nimbusos-docker-keyring must ship
# exactly Docker's key at the path docker.list.binary names in Signed-By.
nimbus_check_docker_keyring_deb() {
	local deb="$1" tmp docker
	nimbus_mktemp_dir tmp
	dpkg-deb -x "$deb" "$tmp"
	[[ -s "$tmp$NIMBUS_DOCKER_KEYRING_PATH" ]] || die "$(basename "$deb") does not ship $NIMBUS_DOCKER_KEYRING_PATH."
	docker="$(nimbus_key_fingerprints "$tmp$NIMBUS_DOCKER_KEYRING_PATH")"
	[[ "$docker" == "$NIMBUS_DOCKER_KEY_FPR" ]] \
		|| die "$(basename "$deb") ships a Docker keyring with fingerprint '${docker:-none}', expected $NIMBUS_DOCKER_KEY_FPR."
}

# -----------------------------------------------------------------------------
# Private APT configurations (build.sh --from-repo, tools/verify-trixie.sh)
# -----------------------------------------------------------------------------
# nimbus_apt_private_init DIR — set up an APT configuration that lives
# entirely in DIR: its own sources, lists, cache, trusted keys and an empty
# dpkg status (so dependency resolution starts from nothing, like a fresh
# chroot). The host's APT setup is neither read nor changed. Callers add
# DIR/etc/sources.list.d/*.sources and run:  APT_CONFIG=DIR/apt.conf apt-get ...
nimbus_apt_private_init() {
	local dir="$1"
	mkdir -p "$dir/etc/apt.conf.d" "$dir/etc/sources.list.d" "$dir/etc/preferences.d" \
		"$dir/etc/trusted.gpg.d" "$dir/state/lists/partial" "$dir/cache/archives/partial"
	: >"$dir/etc/sources.list"
	: >"$dir/status"
	cat >"$dir/apt.conf" <<EOF
// Private APT configuration created by build/lib/common.sh.
Dir::Etc "${dir}/etc";
Dir::State "${dir}/state";
Dir::State::status "${dir}/status";
Dir::Cache "${dir}/cache";
APT::Architecture "${NIMBUS_ARCH}";
APT::Architectures { "${NIMBUS_ARCH}"; };
APT::Sandbox::User "root";
Acquire::Retries "3";
EOF
}

# nimbus_debian_keyring — path of the Debian archive keyring on this host
# (Trixie names it .pgp; older releases used .gpg).
nimbus_debian_keyring() {
	local k
	for k in /usr/share/keyrings/debian-archive-keyring.pgp /usr/share/keyrings/debian-archive-keyring.gpg; do
		if [[ -s "$k" ]]; then
			printf '%s\n' "$k"
			return 0
		fi
	done
	die "The Debian archive keyring is missing; install debian-archive-keyring."
}

# -----------------------------------------------------------------------------
# live-build package lists
# -----------------------------------------------------------------------------
# nimbus_parse_package_list FILE — print the package names in a live-build
# package list, one per line, after checking the format rules that
# live-build's Expand_packagelist imposes (live-build 1:20250505):
#   * the file is passed through printf as a FORMAT string, so a percent sign
#     or a backslash anywhere breaks it;
#   * a line starting with "!" is executed as a shell command;
#   * "#if ", "#nif " and "#endif" are conditionals, every other line starting
#     with "#" is a comment;
#   * there are no trailing comments: "pkg # note" would install "#" and "note".
# NimbusOS lists use one package name per line and plain "# " comments only.
nimbus_parse_package_list() {
	local file="$1" line n=0 name_re='^[a-z0-9][a-z0-9+.-]+$'
	if grep -q '[%\\]' -- "$file"; then
		die "$file contains a percent sign or a backslash; live-build passes package lists through printf."
	fi
	while IFS= read -r line || [[ -n "$line" ]]; do
		n=$((n + 1))
		line="${line#"${line%%[![:space:]]*}"}"      # trim leading whitespace
		line="${line%"${line##*[![:space:]]}"}"      # trim trailing whitespace
		case "$line" in
			"") continue ;;
			"#if "*|"#nif "*|"#endif"*)
				die "$file:$n: live-build conditionals are not used in NimbusOS lists: $line" ;;
			"#"*) continue ;;
			"!"*)
				die "$file:$n: a line starting with ! would be run as a shell command by live-build." ;;
		esac
		[[ "$line" =~ $name_re ]] \
			|| die "$file:$n: expected one package name per line (no trailing comments), got: $line"
		printf '%s\n' "$line"
	done <"$file"
}

# nimbus_list_role FILE — how nimbusos-desktop lists a .list.chroot's
# packages (docs/PLAN.md NOTE-48), from its "#! nimbus: <role>" header line:
#   depends      no marker: NimbusOS needs these to work
#   recommends   "#! nimbus: desktop-recommends": users may remove them
#   exclude      "#! nimbus: exclude-from-desktop": Calamares (removes itself
#                after install) and the metapackage's own list
nimbus_list_role() {
	local markers
	markers="$(grep -E '^#! nimbus: ' -- "$1" || true)"
	case "$markers" in
		"")                                 echo depends ;;
		"#! nimbus: desktop-recommends")    echo recommends ;;
		"#! nimbus: exclude-from-desktop")  echo exclude ;;
		*) die "$1: unknown or repeated marker line(s): $(echo "$markers" | tr '\n' ' ')" ;;
	esac
}

# -----------------------------------------------------------------------------
# Final image checks (run by build.sh after lb build)
# -----------------------------------------------------------------------------
# nimbus_check_image_apt SQUASHFS — the image must carry the final Docker
# source from docker.list.binary (Signed-By the packaged keyring), and no
# trace of the build-time source or the build-only key (docs/PLAN.md NOTE-03).
nimbus_check_image_apt() {
	local sq="$1" tmp root list f fprs line
	local -a problems=()
	nimbus_mktemp_dir tmp
	root="$tmp/root"
	unsquashfs -no-progress -no-xattrs -dest "$root" "$sq" etc/apt usr/share/keyrings >/dev/null

	list="$root/etc/apt/sources.list.d/docker.list"
	if [[ ! -f "$list" ]]; then
		problems+=("/etc/apt/sources.list.d/docker.list (from config/archives/docker.list.binary) is missing")
	else
		grep -q "$NIMBUS_DOCKER_LIST_BINARY_MARKER" "$list" \
			|| problems+=("/etc/apt/sources.list.d/docker.list is not the docker.list.binary source")
		while IFS= read -r line; do
			[[ "$line" == *"signed-by=${NIMBUS_DOCKER_KEYRING_PATH}"* ]] \
				|| problems+=("docker.list line without signed-by=${NIMBUS_DOCKER_KEYRING_PATH}: $line")
		done < <(grep -E '^[[:space:]]*deb(-src)?[[:space:]]' "$list" || true)
	fi
	for f in "$root"/etc/apt/sources.list "$root"/etc/apt/sources.list.d/*; do
		[[ -f "$f" ]] || continue
		if grep -q "$NIMBUS_DOCKER_LIST_CHROOT_MARKER" "$f"; then
			problems+=("${f#"$root"} is the build-time docker.list.chroot")
		fi
		if [[ "$f" != "$list" ]] && grep -qE '^[^#]*download\.docker\.com' "$f"; then
			problems+=("${f#"$root"} adds a second Docker source")
		fi
	done
	for f in "$root"/etc/apt/trusted.gpg "$root"/etc/apt/trusted.gpg.d/*; do
		[[ -f "$f" ]] || continue
		if [[ "$(basename "$f")" == *docker* ]]; then
			problems+=("build-only key left in the image: ${f#"$root"}")
			continue
		fi
		fprs="$(nimbus_key_fingerprints "$f")"
		if [[ "$fprs" == *"$NIMBUS_DOCKER_KEY_FPR"* ]]; then
			problems+=("Docker's key is globally trusted through ${f#"$root"}")
		fi
	done
	f="$root$NIMBUS_DOCKER_KEYRING_PATH"
	if [[ ! -s "$f" ]]; then
		problems+=("$NIMBUS_DOCKER_KEYRING_PATH (nimbusos-docker-keyring) is missing")
	elif [[ "$(nimbus_key_fingerprints "$f")" != "$NIMBUS_DOCKER_KEY_FPR" ]]; then
		problems+=("$NIMBUS_DOCKER_KEYRING_PATH does not hold exactly Docker's key")
	fi

	if ((${#problems[@]})); then
		printf '[%s] ERROR: image APT check failed for %s:\n' "$_nimbus_prog" "$sq" >&2
		printf '  - %s\n' "${problems[@]}" >&2
		exit 1
	fi
	log "Image APT check OK: docker.list from docker.list.binary, Signed-By $NIMBUS_DOCKER_KEYRING_PATH, no build-only key."
}

# nimbus_prepare_grub_theme CONFIG_DIR BRANDING_DEB — puts the GRUB theme of
# the nimbusos-branding package into CONFIG_DIR/bootloaders/grub-pc/live-theme,
# so live and installed boot share one theme. live-build copies its own
# templates first and config/bootloaders/{grub-pc,isolinux,syslinux_common}/
# on top (binary_grub_cfg, binary_syslinux in live-build 1:20250505); the
# NimbusOS theme.cfg loads this theme's fonts (docs/PLAN.md NOTE-64).
nimbus_prepare_grub_theme() {
	local config="$1" deb="$2" dir extract theme
	[[ -f "$deb" ]] || die "nimbus_prepare_grub_theme: no nimbusos-branding package at '$deb'."
	dir="$config/bootloaders/grub-pc"
	nimbus_mktemp_dir extract
	dpkg-deb -x "$deb" "$extract"
	theme="$extract/usr/share/grub/themes/$NIMBUS_ID"
	[[ -f "$theme/theme.txt" ]] || die "nimbusos-branding does not contain /usr/share/grub/themes/$NIMBUS_ID/theme.txt."
	rm -rf -- "$dir/live-theme"
	mkdir -p -- "$dir/live-theme"
	cp -R --preserve=mode,timestamps -- "$theme/." "$dir/live-theme/"
	log "bootloaders/grub-pc/live-theme taken from $(basename "$deb")"
}

# nimbus_check_image_initrd INITRD — the live initramfs must show the NimbusOS
# boot splash: the nimbusos Plymouth theme and Plymouth's script plugin.
# live-build regenerates the initramfs after the hooks (chroot_hacks), so
# this checks its result instead of a hook of its own (docs/PLAN.md NOTE-62).
nimbus_check_image_initrd() {
	local initrd="$1" listing path
	local -a problems=()
	[[ -s "$initrd" ]] || die "image initrd check: $initrd is missing or empty."
	listing="$(lsinitramfs "$initrd" 2>/dev/null)" || die "image initrd check: lsinitramfs cannot read $initrd."
	for path in nimbusos.plymouth nimbusos.script logo.png progress-bar.png; do
		grep -qE "(^|/)usr/share/plymouth/themes/nimbusos/${path//./\\.}\$" <<<"$listing" \
			|| problems+=("usr/share/plymouth/themes/nimbusos/$path is not in the initramfs")
	done
	grep -qE '(^|/)plymouth/script\.so$' <<<"$listing" \
		|| problems+=("Plymouth's script plugin (plymouth/script.so) is not in the initramfs")
	if ((${#problems[@]})); then
		printf '[%s] ERROR: image initrd check failed for %s:\n' "$_nimbus_prog" "$initrd" >&2
		printf '  - %s\n' "${problems[@]}" >&2
		printf '  The boot splash would not be NimbusOS. Is nimbusos-branding installed and its Plymouth theme selected?\n' >&2
		exit 1
	fi
	log "Image initrd check OK: the NimbusOS Plymouth theme and the script plugin are in $(basename "$initrd")."
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
		warn "$url has no Valid-Until field; tools/publish-apt.sh normally writes one."
	fi
	log "Repository OK: signed by a committed key, valid until ${valid_until:-<not set>}."
}

# -----------------------------------------------------------------------------
# File modes of staged copies: taken from git, never from the file system
# -----------------------------------------------------------------------------
# nimbus_apply_git_modes DEST PREFIX — DEST is a staged copy of the repository
# directory PREFIX. Directories become 0755; every file becomes 0644, except
# files git records as executable (100755), which become 0755. Files git
# doesn't track are staged as 0644 with a warning.
# Why: a checkout on a Windows file system (WSL /mnt/c, Docker Desktop) shows
# every file as 0777, and debhelper runs an executable debian/install as a
# script. git's index is the one record of which files are meant to be
# executable (tools/check-file-modes.sh keeps it right), so a package is
# identical whichever host it was built on.
nimbus_apply_git_modes() {
	local dest="$1" prefix="$2" entry mode path rel
	local -a untracked=()
	if ! nimbus_git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		warn "Not a git checkout: the file modes of $prefix are taken from the file system."
		return 0
	fi
	find "$dest" -type d -exec chmod 0755 {} +
	find "$dest" -type f -exec chmod 0644 {} +
	while IFS= read -r -d '' entry; do
		mode="${entry%% *}"                      # "<mode> <object> <stage>\t<path>"
		path="${entry#*$'\t'}"
		rel="${path#"$prefix"/}"
		if [[ "$mode" == 100755 && -f "$dest/$rel" ]]; then
			chmod 0755 -- "$dest/$rel"
		fi
	done < <(nimbus_git ls-files -s -z -- "$prefix")
	mapfile -d '' -t untracked < <(nimbus_git ls-files -z --others --exclude-standard -- "$prefix")
	if ((${#untracked[@]})); then
		warn "Not tracked by git, so staged as 0644: ${untracked[*]}. Run git add (with --chmod=+x for scripts)."
	fi
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
	local display_version="$1" volume live append failsafe
	volume="${NIMBUS_ISO_VOLUME_PREFIX} ${display_version}"
	volume="${volume:0:32}"                     # ISO 9660 volume IDs are at most 32 characters

	# Kernel command line of the live session (read by live-boot/live-config).
	live="hostname=${NIMBUS_HOSTNAME}"
	live+=" username=${NIMBUS_LIVE_USERNAME}"
	live+=" user-fullname=${NIMBUS_LIVE_USER_FULLNAME}"
	live+=" user-default-groups=${NIMBUS_LIVE_USER_GROUPS}"   # docs/PLAN.md §2 (users and groups)
	live+=" locales=${NIMBUS_LOCALE}"
	live+=" keyboard-layouts=${NIMBUS_KEYBOARD_LAYOUTS}"
	live+=" timezone=${NIMBUS_TIMEZONE}"
	append="boot=live components ${NIMBUS_BOOT_EXTRA} ${live}"
	# The fail-safe entries: live-build's own fail-safe options, plus the same
	# live user and settings (live-build's default line drops them, which
	# would leave the live user without the Control Center groups; NOTE-64).
	failsafe="boot=live components ${live} memtest noapic noapm nodma nomce nosmp nosplash vga=788"

	# The result, read by build.sh and tools/verify-trixie.sh.
	# shellcheck disable=SC2034
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

		# CA certificates in the bootstrapped system: Docker's source
		# (config/archives/docker.list.chroot) is HTTPS, and apt in the fresh
		# chroot can't verify it without them ("certificate verify failed",
		# so docker-ce can't be found). live-build adds them by itself only
		# when the Debian mirror is HTTPS (docs/PLAN.md NOTE-72).
		--debootstrap-options "--include=ca-certificates"

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
		--bootappend-live-failsafe "$failsafe"
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
# seven packages, all with the same version. Prints that version.
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
