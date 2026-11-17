#!/bin/bash
# =============================================================================
# tools/dev-shell.sh — a throwaway Debian 13 container for building and testing
# =============================================================================
# What: starts NIMBUS_DEV_IMAGE (debian:trixie, set in build/nimbus.conf)
#       with Docker or Podman, as root. Unless --no-setup is given, it first
#       installs the basic developer tools (git, dpkg-dev, debhelper,
#       devscripts, gpg, gpg-agent, gpgv, curl, ca-certificates). Then it runs
#       COMMAND, or an interactive shell. The container is deleted on exit.
#
#       Default, clone mode: the checkout is mounted read-only at /src and
#       cloned to /work inside the container, which is where the command
#       runs. Uncommitted changes to tracked files are carried over as a
#       patch, and untracked files are copied as 0644 with a warning. Build
#       outputs go back to the host through two mounts:
#           <repo>/dist        -> /out/dist   (NIMBUS_OUTPUT_DIR: ISOs)
#           <repo>/_build/debs -> /out/debs   (NIMBUS_DEBS_DIR: packages)
#       --mount mode: the command runs directly in the read-write mounted
#       checkout at /src. Use it only for tools that must write into the
#       repository, such as tools/fetch-docker-key.sh --write.
#
# Why:  building needs a real Debian 13 userland. A clone on the container's
#       own file system is much faster than a Windows mount (WSL /mnt/c) and
#       carries the file modes recorded in git; a Windows mount shows every
#       file as executable. Nothing on the host changes except dist/ and
#       _build/debs/.
#
# Usage: tools/dev-shell.sh [options] [-- COMMAND [ARGS...]]
#   --privileged   run privileged; needed for build/build.sh (live-build
#                  mounts /proc, /sys and /dev/pts inside its chroot)
#   --mount        run in the mounted checkout instead of a clone
#   --no-setup     do not install the developer tools first
#   --engine NAME  docker or podman (default: docker if it works, else podman)
#   -h, --help     show this help
# Examples:
#   tools/dev-shell.sh                                      interactive shell
#   tools/dev-shell.sh -- tools/tests/deb-version.test.sh   version mapping tests
#   tools/dev-shell.sh -- tools/verify-trixie.sh            archive check
#   tools/dev-shell.sh --privileged -- build/build.sh       build the ISO into dist/
#   tools/dev-shell.sh --mount -- tools/fetch-docker-key.sh --write
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
prepare_clone() {
	local patch=/tmp/nimbus-worktree.patch f
	local -a untracked
	log "Cloning /src to /work"
	git clone -q /src /work
	git -C /src diff HEAD --binary >"$patch"
	if [[ -s "$patch" ]]; then
		# --index: new files and mode changes enter the clone's index, so
		# builds there see the modes git records (nimbus_apply_git_modes).
		git -C /work apply --index --whitespace=nowarn "$patch"
		log "Applied uncommitted changes to tracked files ($(grep -c '^diff --git' "$patch") files)."
	fi
	mapfile -d '' -t untracked < <(git -C /src ls-files -z --others --exclude-standard)
	for f in "${untracked[@]}"; do
		mkdir -p -- "/work/$(dirname -- "$f")"
		install -m 0644 -- "/src/$f" "/work/$f"
	done
	if ((${#untracked[@]})); then
		warn "Copied untracked files as 0644 (git add them to record their mode): ${untracked[*]}"
	fi
}

run_inside() {
	local setup="$1" mode="$2"
	shift 2
	if [[ "$setup" == 1 ]]; then
		# gpg-agent: gpg only Recommends it, but generating keys (tests,
		# --allow-test-key) needs it.
		nimbus_apt_install_missing git ca-certificates curl gpg gpg-agent gpgv dpkg-dev debhelper devscripts
	elif [[ "$mode" == clone ]]; then
		nimbus_apt_install_missing git
	fi
	if [[ "$mode" == clone ]]; then
		prepare_clone
		cd /work
	fi
	if (($#)); then
		exec "$@"
	fi
	exec bash -l
}

# --- On the host ------------------------------------------------------------
run_host() {
	local engine="$1" privileged="$2" setup="$3" mode="$4" var
	shift 4
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
		# The checkout belongs to another uid inside the container; trust it.
		--env GIT_CONFIG_COUNT=1 --env GIT_CONFIG_KEY_0=safe.directory --env GIT_CONFIG_VALUE_0=/src)
	if [[ "$mode" == clone ]]; then
		mkdir -p -- "$NIMBUS_REPO_ROOT/dist" "$NIMBUS_REPO_ROOT/_build/debs"
		args+=(--volume "$NIMBUS_REPO_ROOT:/src:ro,z" --workdir /
			--volume "$NIMBUS_REPO_ROOT/dist:/out/dist:z"
			--volume "$NIMBUS_REPO_ROOT/_build/debs:/out/debs:z")
		# Only if the user didn't export their own (nimbus_load_conf sets
		# defaults in this shell, so check the environment, not the variable).
		printenv NIMBUS_OUTPUT_DIR >/dev/null || args+=(--env NIMBUS_OUTPUT_DIR=/out/dist)
		printenv NIMBUS_DEBS_DIR >/dev/null || args+=(--env NIMBUS_DEBS_DIR=/out/debs)
	else
		args+=(--volume "$NIMBUS_REPO_ROOT:/src:z" --workdir /src)
	fi
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

	log "Starting $NIMBUS_DEV_IMAGE with $engine ($mode mode)"
	# Not exec: this shell must stay to clean up its temporary files. The
	# container's exit status is passed on unchanged.
	local rc=0
	"$engine" "${global[@]}" "${args[@]}" "$NIMBUS_DEV_IMAGE" \
		bash /src/tools/dev-shell.sh --inside "$setup" "$mode" -- "$@" || rc=$?
	exit "$rc"
}

main() {
	local engine="" privileged=0 setup=1 mode=clone

	if [[ "${1:-}" == --inside ]]; then
		[[ $# -ge 4 && "$4" == -- ]] || die "internal: bad --inside call"
		local inner_setup="$2" inner_mode="$3"
		shift 4
		run_inside "$inner_setup" "$inner_mode" "$@"
	fi

	while (($#)); do
		case "$1" in
			--privileged) privileged=1; shift ;;
			--mount)      mode=mount; shift ;;
			--no-setup)   setup=0; shift ;;
			--engine)     [[ $# -ge 2 ]] || die "--engine needs a value"; engine="$2"; shift 2 ;;
			-h|--help)    usage; exit 0 ;;
			--)           shift; break ;;
			*) die "Unknown option: $1 (put the command after --; see --help)" ;;
		esac
	done

	nimbus_load_conf
	run_host "$engine" "$privileged" "$setup" "$mode" "$@"
}

main "$@"
