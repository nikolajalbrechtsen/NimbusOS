#!/bin/bash
# =============================================================================
# tools/gen-desktop-depends.sh — nimbusos-desktop's Depends and Recommends, from the package lists
# =============================================================================
# What: reads build/config/package-lists/*.list.chroot and prints the
#       packages of one role as a dependency line ("a, b, c"), the value of
#       the ${nimbus:ListDepends} or ${nimbus:ListRecommends} substvar in
#       packages/nimbusos-desktop. A list's role comes from its header line
#       (build/lib/common.sh nimbus_list_role):
#         no marker                        -> Depends
#         #! nimbus: desktop-recommends    -> Recommends
#         #! nimbus: exclude-from-desktop  -> left out
#       *.list.binary files (the ISO's package pool) are never read, and
#       NimbusOS's own packages are left out (nimbusos-desktop names those
#       itself, with version constraints).
#
# Why:  spec deliverable 3 and docs/PLAN.md NOTE-48. The image and installed
#       systems must never drift apart, and a package users may reasonably
#       remove (a browser, Konsole, optional firmware) must not take
#       nimbusos-desktop with it, or that machine would silently stop
#       receiving new NimbusOS software. tools/build-debs.sh runs this at
#       build time; nothing generated is committed.
#
# Usage: tools/gen-desktop-depends.sh [--depends | --recommends | --list]
#   --depends     (default) the Depends line
#   --recommends  the Recommends line
#   --list        every package with its role and list, for review
#   -h, --help    show this help
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

main() {
	local mode="depends" dir list pkg own parsed role want
	local -a lists pkgs
	local -A seen=() role_of=() is_own=()

	while (($#)); do
		case "$1" in
			--depends)    mode="depends"; shift ;;
			--recommends) mode="recommends"; shift ;;
			--list)       mode="list"; shift ;;
			-h|--help)    usage; exit 0 ;;
			*) die "Unknown option: $1 (see --help)" ;;
		esac
	done

	for own in "${NIMBUS_PACKAGES[@]}"; do
		is_own["$own"]=1
	done

	dir="$NIMBUS_REPO_ROOT/build/config/package-lists"
	mapfile -t lists < <(find "$dir" -maxdepth 1 -type f -name '*.list.chroot' | LC_ALL=C sort)
	((${#lists[@]})) || die "No *.list.chroot files in $dir."

	for list in "${lists[@]}"; do
		role="$(nimbus_list_role "$list")"
		[[ "$role" != exclude ]] || continue
		# Captured first: a format error in the list must stop this script,
		# which a failure inside < <(...) would not.
		parsed="$(nimbus_parse_package_list "$list")"
		[[ -n "$parsed" ]] || continue
		mapfile -t pkgs <<<"$parsed"
		for pkg in "${pkgs[@]}"; do
			[[ -z "${is_own[$pkg]:-}" ]] || continue
			if [[ -n "${seen[$pkg]:-}" ]]; then
				die "$pkg is listed twice: in ${seen[$pkg]} and $(basename "$list"). Keep it in one list."
			fi
			seen["$pkg"]="$(basename "$list")"
			role_of["$pkg"]="$role"
		done
	done
	((${#seen[@]})) || die "The package lists produced no dependencies."

	if [[ "$mode" == list ]]; then
		for pkg in "${!seen[@]}"; do
			printf '%-40s %-11s %s\n' "$pkg" "${role_of[$pkg]}" "${seen[$pkg]}"
		done | LC_ALL=C sort
		return 0
	fi
	want="$mode"
	for pkg in "${!seen[@]}"; do
		if [[ "${role_of[$pkg]}" == "$want" ]]; then
			printf '%s\n' "$pkg"
		fi
	done | LC_ALL=C sort | paste -sd ',' - | sed 's/,/, /g'
}

main "$@"
