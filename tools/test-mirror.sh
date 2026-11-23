#!/bin/bash
# =============================================================================
# tools/test-mirror.sh: a throwaway NimbusOS APT repository for QEMU tests
# =============================================================================
# What: rehearses the release flow of docs/RELEASING.md on this machine, with
#       a throwaway signing key, so the QEMU smoke test (docs/TESTING.md)
#       can install from a "v0.1.0" ISO, update to "v0.1.1-beta.1" and meet
#       a broken signature without publishing anything and without the real
#       signing key. Everything lives in DIR (default:
#       NIMBUS_OUTPUT_DIR/test-mirror, which is dist/test-mirror, or
#       /out/dist/test-mirror inside tools/dev-shell.sh):
#         DIR/key/secret.asc           the throwaway secret key (no passphrase)
#         DIR/key/public/*.asc         its public half; the packages trust it
#         DIR/site/apt/                the repository, written by tools/publish-apt.sh
#         DIR/debs/TAG/                the packages built for TAG
#         DIR/iso/TAG/                 the ISO built from them
#       Packages and ISOs built here trust only the throwaway key and point at
#       http://10.0.2.2:PORT/apt. 10.0.2.2 is the address under which a QEMU
#       guest with user networking reaches the host's loopback, so they work
#       only inside the QEMU test. The ISO is named nimbusos-test-amd64.iso
#       and its volume label starts with "NimbusOS TEST".
#
#       Commands:
#         init              create the key and an empty, signed repository
#                           (both suites, like refresh.yml's first run)
#         publish TAG       build the seven packages for release tag TAG
#                           (vX.Y.Z or vX.Y.Z-beta.N; needs a clean git tree,
#                           like a real tag) and publish them as release.yml
#                           does: suite stable or beta, pruning, releases.json.
#                           The notes are docs/release-notes/TAG.md if it
#                           exists, else a generated test note.
#         iso TAG           build the ISO from TAG's published packages with
#                           build/build.sh. Root, in a privileged container
#                           (tools/dev-shell.sh --privileged): the mirror's
#                           address is added to the container's loopback and
#                           served there while live-build runs (NOTE-04)
#         serve             serve DIR/site on 127.0.0.1:PORT in the foreground.
#                           Run it on the machine that runs QEMU
#         break-signature [SUITE]
#                           change one character of the signed text of
#                           dists/SUITE/InRelease (default stable), so its
#                           signature no longer matches (smoke test 10)
#         repair            sign every suite again (undoes break-signature)
#         check             what a client checks (tools/publish-apt.sh check)
#         env               print the NIMBUS_* settings that point a build at
#                           this mirror, for: eval "$(tools/test-mirror.sh env)"
#
# Why:  spec, deliverable 11, steps 8 to 10: publish v0.1.1-beta.1 and update
#       to it, prove the pinning with apt-cache policy, and corrupt InRelease
#       "on a test mirror". A local mirror with its own key does all three
#       without touching the real repository or the real key (NOTE-72).
#
# Usage: tools/test-mirror.sh [--dir DIR] [--port PORT] COMMAND [ARGS]
#   --dir DIR    the mirror's directory (default: NIMBUS_OUTPUT_DIR/test-mirror)
#   --port PORT  the port QEMU guests use, 1024 to 65535 (default 8080)
#   Debian 13 with gpg, gpgv, apt-utils, dpkg-dev, xz-utils and python3
#   (tools/dev-shell.sh has them, or installs them). publish and iso build
#   packages, so they run where build-debs.sh and build.sh run.
# Examples (docs/TESTING.md):
#   tools/dev-shell.sh -- tools/test-mirror.sh init
#   tools/dev-shell.sh -- tools/test-mirror.sh publish v0.1.0
#   sudo tools/dev-shell.sh --privileged -- tools/test-mirror.sh iso v0.1.0
#   tools/test-mirror.sh serve          (on the QEMU host, in its own terminal)
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# The QEMU user-network address of the host (QEMU's "-netdev user" default).
GUEST_HOST="10.0.2.2"

dir=""
port="8080"
fpr=""

# --- settings -----------------------------------------------------------------
# mirror_env: point every NIMBUS_* setting that ends up in packages or the ISO
# at the mirror and its key. Exported, so build-debs.sh, publish-apt.sh and
# build.sh (which load nimbus.conf again; the environment wins) agree.
mirror_env() {
	export NIMBUS_APT_URL="http://${GUEST_HOST}:${port}/apt"
	# apt's "Pin: origin" and nimbus-update compare the host without the port.
	export NIMBUS_APT_HOST="$GUEST_HOST"
	export NIMBUS_RELEASES_JSON_URL="${NIMBUS_APT_URL}/releases.json"
	export NIMBUS_ARCHIVE_KEYS_DIR="$dir/key/public"
	export NIMBUS_ISO_BASENAME="${NIMBUS_ID}-test-${NIMBUS_ARCH}"
	export NIMBUS_ISO_VOLUME_PREFIX="${NIMBUS_NAME} TEST"
}

print_env() {
	local v
	for v in NIMBUS_APT_URL NIMBUS_APT_HOST NIMBUS_RELEASES_JSON_URL NIMBUS_ARCHIVE_KEYS_DIR \
		NIMBUS_ISO_BASENAME NIMBUS_ISO_VOLUME_PREFIX; do
		printf 'export %s=%q\n' "$v" "${!v}"
	done
}

# use_key: import the throwaway key into a private GNUPGHOME and set fpr.
use_key() {
	[[ -s "$dir/key/secret.asc" ]] || die "$dir has no key yet. Run: $0 init"
	nimbus_mktemp_dir GNUPGHOME
	export GNUPGHOME
	chmod 0700 "$GNUPGHOME"
	gpg --batch --quiet --import "$dir/key/secret.asc" 2>/dev/null
	fpr="$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
	[[ -n "$fpr" ]] || die "$dir/key/secret.asc holds no secret key."
	export APT_SIGNING_PASSPHRASE=""
}

check_tag() {
	nimbus_tag_to_debian "$1" >/dev/null || die "'$1' is not a release tag. Use vX.Y.Z or vX.Y.Z-beta.N."
}

in_container() {
	[[ -f /.dockerenv || -f /run/.containerenv ]]
}

# need_tools: what publish-apt.sh and the key handling need. Installed when
# running as root (tools/dev-shell.sh), otherwise only checked.
need_tools() {
	local -a pkgs=(gpg gpg-agent gpgv apt-utils dpkg-dev xz-utils python3)
	if [[ "$(id -u)" -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
		nimbus_apt_install_missing "${pkgs[@]}"
		return 0
	fi
	local cmd
	for cmd in gpg gpgv apt-ftparchive dpkg-scanpackages xz python3; do
		command -v "$cmd" >/dev/null 2>&1 || die "$cmd is not installed. Install: ${pkgs[*]} (or run this in tools/dev-shell.sh)."
	done
}

# --- commands -------------------------------------------------------------------
do_init() {
	local tmp
	need_tools
	mkdir -p -- "$dir"
	# Belt and braces: nothing in here may ever be committed (dist/ is
	# ignored too).
	printf '*\n' >"$dir/.gitignore"
	if [[ -s "$dir/key/secret.asc" ]]; then
		log "Reusing the throwaway key in $dir/key/"
	else
		step "Throwaway signing key"
		mkdir -p -- "$dir/key/public"
		chmod 0700 "$dir/key"
		nimbus_mktemp_dir tmp
		chmod 0700 "$tmp"
		GNUPGHOME="$tmp" gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
			--quick-generate-key "${NIMBUS_NAME} test mirror (throwaway key, QEMU tests only)" ed25519 sign 1y 2>/dev/null
		GNUPGHOME="$tmp" gpg --batch --armor --export >"$dir/key/public/test-mirror.asc"
		(umask 077 && GNUPGHOME="$tmp" gpg --batch --armor --pinentry-mode loopback --passphrase '' \
			--export-secret-keys >"$dir/key/secret.asc")
		log "Created $(nimbus_key_fingerprints "$dir/key/public/test-mirror.asc")"
	fi
	use_key
	if [[ -f "$dir/site/apt/dists/$NIMBUS_APT_DEFAULT_SUITE/InRelease" ]]; then
		log "The repository exists already: $dir/site/apt"
	else
		step "Empty repository"
		"$NIMBUS_REPO_ROOT/tools/publish-apt.sh" init --site "$dir/site" --key "$fpr"
	fi
	"$NIMBUS_REPO_ROOT/tools/publish-apt.sh" check --site "$dir/site"
	log "Ready. Build a release into it: $0 publish v0.1.0"
}

do_publish() {
	local tag="${1:?publish needs a release tag, for example v0.1.0}" notes commit
	local -a deps=()
	check_tag "$tag"
	need_tools
	use_key
	if [[ "$(id -u)" -eq 0 ]]; then
		deps=(--install-build-deps)
	fi
	step "Packages for $tag"
	"$NIMBUS_REPO_ROOT/tools/build-debs.sh" --tag "$tag" --out "$dir/debs/$tag" "${deps[@]}"

	notes="$NIMBUS_REPO_ROOT/docs/release-notes/$tag.md"
	if [[ ! -f "$notes" ]]; then
		commit="$(nimbus_git rev-parse --short=7 HEAD)"
		mkdir -p -- "$dir/notes"
		notes="$dir/notes/$tag.md"
		printf '# %s %s (test mirror)\n\nA rehearsal build of commit %s, made by tools/test-mirror.sh for the QEMU test in docs/TESTING.md. It exists only on this test mirror.\n' \
			"$NIMBUS_NAME" "${tag#v}" "$commit" >"$notes"
	fi
	step "Publishing $tag"
	"$NIMBUS_REPO_ROOT/tools/publish-apt.sh" add --site "$dir/site" --key "$fpr" --tag "$tag" \
		--debs "$dir/debs/$tag" --notes "$notes"
	"$NIMBUS_REPO_ROOT/tools/publish-apt.sh" check --site "$dir/site"
}

do_iso() {
	local tag="${1:?iso needs a release tag that was published, for example v0.1.0}"
	local debs added="" server_pid rc=0
	check_tag "$tag"
	debs="$dir/debs/$tag"
	compgen -G "$debs/nimbusos-desktop_*.deb" >/dev/null || die "No packages for $tag in $debs. Run first: $0 publish $tag"
	nimbus_require_root
	in_container || die "iso adds the mirror's address $GUEST_HOST to this machine's loopback while live-build runs. Run it in a throwaway container, which has its own network: sudo tools/dev-shell.sh --privileged -- $0 iso $tag"
	nimbus_apt_install_missing python3 iproute2

	# A QEMU guest reaches the mirror at http://GUEST_HOST:PORT; during the
	# build the same URL must answer here (build.sh's repository check and
	# live-build's last apt-get update, NOTE-04).
	if ip -4 route show default | grep -qw "via $GUEST_HOST"; then
		die "$GUEST_HOST is this container's own gateway (rootless Podman with slirp4netns), so it can't also be the mirror. Run the container as root (sudo) or with Docker."
	fi
	if ! ip -4 -o addr show | grep -q " ${GUEST_HOST}/"; then
		ip addr add "${GUEST_HOST}/32" dev lo
		added=1
	fi
	python3 -m http.server "$port" --bind "$GUEST_HOST" --directory "$dir/site" >"$dir/iso-http.log" 2>&1 &
	server_pid=$!
	for _ in $(seq 50); do
		if (exec 3<>"/dev/tcp/$GUEST_HOST/$port") 2>/dev/null; then break; fi
		sleep 0.1
	done

	step "ISO for $tag from $NIMBUS_APT_URL"
	"$NIMBUS_REPO_ROOT/build/build.sh" --debs-dir "$debs" --output "$dir/iso/$tag" || rc=$?

	kill "$server_pid" 2>/dev/null || true
	if [[ -n "$added" ]]; then
		ip addr del "${GUEST_HOST}/32" dev lo || true
	fi
	((rc == 0)) || die "build/build.sh failed (exit status $rc)."
	log "ISO: $dir/iso/$tag/${NIMBUS_ISO_BASENAME}.iso"
}

do_serve() {
	[[ -d "$dir/site/apt/dists" ]] || die "$dir has no repository yet. Run: $0 init"
	command -v python3 >/dev/null 2>&1 || die "python3 is needed to serve the mirror."
	log "Serving $dir/site on 127.0.0.1:$port. QEMU guests reach it as $NIMBUS_APT_URL/ . Stop with Ctrl+C."
	exec python3 -m http.server "$port" --bind 127.0.0.1 --directory "$dir/site"
}

do_break() {
	local suite="${1:-$NIMBUS_APT_DEFAULT_SUITE}" file tmp
	file="$dir/site/apt/dists/$suite/InRelease"
	[[ -f "$file" ]] || die "$file does not exist."
	grep -qE '^Date: (Mon|Tue|Wed|Thu|Fri|Sat|Sun)' "$file" || die "$file has no Date: line to change."
	# The Date line is inside the signed text: changing its weekday keeps the
	# file well-formed, so apt reaches the signature check and fails there.
	sed -i -E '0,/^Date: (Mon|Tue|Wed|Thu|Fri|Sat|Sun)/s//Date: Xxx/' "$file"
	nimbus_mktemp_dir tmp
	nimbus_dearmor_into "$tmp/test-mirror.gpg" "$dir/key/public/test-mirror.asc"
	if gpgv --keyring "$tmp/test-mirror.gpg" "$file" >/dev/null 2>&1; then
		die "$file still verifies; it was not changed."
	fi
	log "dists/$suite/InRelease no longer matches its signature. Undo with: $0 repair"
}

do_repair() {
	need_tools
	use_key
	"$NIMBUS_REPO_ROOT/tools/publish-apt.sh" resign --site "$dir/site" --key "$fpr"
	"$NIMBUS_REPO_ROOT/tools/publish-apt.sh" check --site "$dir/site"
}

main() {
	local cmd=""
	while (($#)); do
		case "$1" in
			--dir)  dir="${2:?--dir needs a directory}"; shift 2 ;;
			--port) port="${2:?--port needs a number}"; shift 2 ;;
			-h|--help) usage; exit 0 ;;
			-*) die "Unknown option: $1 (see --help)" ;;
			*) cmd="$1"; shift; break ;;
		esac
	done
	[[ -n "$cmd" ]] || { usage >&2; exit 2; }
	if ! [[ "$port" =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1024 || 10#$port > 65535)); then
		die "--port must be a number from 1024 to 65535."
	fi
	port=$((10#$port))

	nimbus_load_conf
	dir="$(readlink -m -- "${dir:-$(nimbus_path "$NIMBUS_OUTPUT_DIR")/test-mirror}")"
	mirror_env

	case "$cmd" in
		init)            (($# == 0)) || die "init takes no arguments."; do_init ;;
		publish)         (($# == 1)) || die "publish takes one release tag."; do_publish "$1" ;;
		iso)             (($# == 1)) || die "iso takes one release tag."; do_iso "$1" ;;
		serve)           (($# == 0)) || die "serve takes no arguments."; do_serve ;;
		break-signature) (($# <= 1)) || die "break-signature takes at most one suite."; do_break "${1:-}" ;;
		repair)          (($# == 0)) || die "repair takes no arguments."; do_repair ;;
		check)           (($# == 0)) || die "check takes no arguments."; need_tools; "$NIMBUS_REPO_ROOT/tools/publish-apt.sh" check --site "$dir/site" ;;
		env)             (($# == 0)) || die "env takes no arguments."; print_env ;;
		*) die "Unknown command: $cmd (see --help)" ;;
	esac
}

main "$@"
