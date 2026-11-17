#!/bin/bash
# =============================================================================
# tools/dev-shell.sh — a throwaway Debian 13 container with this repository at /src
# =============================================================================
# What: starts NIMBUS_DEV_IMAGE (debian:trixie, set in build/nimbus.conf)
#       with Docker or Podman. The repository is bind-mounted read-write at
#       /src, which is also the working directory, and you are root inside.
#       Unless --no-setup is given, the container first installs the basic
#       developer tools (git, dpkg-dev, debhelper, devscripts, gpg, gpg-agent,
#       gpgv, curl, ca-certificates). Then it runs COMMAND, or an interactive shell if no
#       command is given. The container is deleted when it exits.
#
# Why:  building and testing need a real Debian 13 userland (dpkg, apt,
#       live-build). This gives the same environment CI uses on any host
#       (Windows with WSL, macOS, other distributions) without changing the
#       host itself.
#
# Usage: tools/dev-shell.sh [options] [-- COMMAND [ARGS...]]
#   --privileged   run privileged; needed for build/build.sh (live-build
#                  mounts /proc, /sys and /dev/pts inside its chroot)
#   --no-setup     do not install the developer tools first
#   --engine NAME  docker or podman (default: docker if it works, else podman)
#   -h, --help     show this help
# Examples:
#   tools/dev-shell.sh                                      interactive shell
#   tools/dev-shell.sh -- tools/tests/deb-version.test.sh   version mapping tests
#   tools/dev-shell.sh -- tools/verify-trixie.sh            archive check
#   tools/dev-shell.sh --privileged -- build/build.sh       build the ISO
# Exported NIMBUS_* variables are passed into the container, so nimbus.conf
# overrides work the same inside.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# --- Inside the container ---------------------------------------------------
run_inside() {
	local setup="$1"
	shift
	if [[ "$setup" == 1 ]]; then
		# gpg-agent: gpg only Recommends it, but generating keys (tests,
		# --allow-test-key) needs it.
		nimbus_apt_install_missing git ca-certificates curl gpg gpg-agent gpgv dpkg-dev debhelper devscripts
	fi
	if (($#)); then
		exec "$@"
	fi
	exec bash -l
}

# --- On the host ------------------------------------------------------------
run_host() {
	local engine="$1" privileged="$2" setup="$3" var
	shift 3
	local -a global=() args=()

	if [[ -z "$engine" ]]; then
		if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
			engine=docker
		elif command -v podman >/dev/null 2>&1; then
			engine=podman
		else
			die "Neither Docker nor Podman is available. Install one of them (on Windows: inside a WSL distribution)."
		fi
	fi
	case "$engine" in
		docker|podman) command -v "$engine" >/dev/null 2>&1 || die "$engine is not installed." ;;
		*) die "--engine must be docker or podman." ;;
	esac
	# Without systemd as init (for example WSL), Podman must manage cgroups itself.
	if [[ "$engine" == podman && ! -d /run/systemd/system ]]; then
		global+=(--cgroup-manager=cgroupfs)
	fi

	args=(run --rm --hostname nimbusos-dev
		--volume "$NIMBUS_REPO_ROOT:/src:z" --workdir /src
		# The checkout belongs to another uid inside the container; trust it.
		--env GIT_CONFIG_COUNT=1 --env GIT_CONFIG_KEY_0=safe.directory --env GIT_CONFIG_VALUE_0=/src)
	if [[ -t 0 && -t 1 ]]; then
		args+=(--interactive --tty)
	fi
	if [[ "$privileged" == 1 ]]; then
		args+=(--privileged)
	fi
	for var in $(compgen -e); do
		if [[ "$var" == NIMBUS_* ]]; then
			args+=(--env "$var=${!var}")
		fi
	done

	log "Starting $NIMBUS_DEV_IMAGE with $engine (repository at /src)"
	# Not exec: this shell must stay to clean up its temporary files. The
	# container's exit status is passed on unchanged.
	local rc=0
	"$engine" "${global[@]}" "${args[@]}" "$NIMBUS_DEV_IMAGE" \
		bash /src/tools/dev-shell.sh --inside "$setup" -- "$@" || rc=$?
	exit "$rc"
}

main() {
	local engine="" privileged=0 setup=1

	if [[ "${1:-}" == --inside ]]; then
		[[ $# -ge 3 && "$3" == -- ]] || die "internal: bad --inside call"
		local inner_setup="$2"
		shift 3
		run_inside "$inner_setup" "$@"
	fi

	while (($#)); do
		case "$1" in
			--privileged) privileged=1; shift ;;
			--no-setup)   setup=0; shift ;;
			--engine)     [[ $# -ge 2 ]] || die "--engine needs a value"; engine="$2"; shift 2 ;;
			-h|--help)    usage; exit 0 ;;
			--)           shift; break ;;
			*) die "Unknown option: $1 (put the command after --; see --help)" ;;
		esac
	done

	nimbus_load_conf
	run_host "$engine" "$privileged" "$setup" "$@"
}

main "$@"
