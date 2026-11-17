#!/bin/bash
# =============================================================================
# tools/gen-desktop-depends.sh — nimbusos-desktop's Depends, from the package lists
# =============================================================================
# What: reads build/config/package-lists/*.list.chroot and prints every
#       package in them as one dependency line ("a, b, c"), the value of the
#       ${nimbus:ListDepends} substvar in packages/nimbusos-desktop. Skipped:
#         * lists marked "#! nimbus: exclude-from-desktop-depends"
#           (Calamares, which removes itself after install, and the
#           metapackage's own list);
#         * *.list.binary files (they fill the ISO's package pool, not the
#           installed system);
#         * NimbusOS's own packages (nimbusos-desktop names those itself,
#           with version constraints).
#
# Why:  spec deliverable 3. The image (package lists) and installed systems
#       (the metapackage) must never drift apart. Adding a package to a list
#       is how new software reaches existing installs (docs/PLAN.md NOTE-48).
#       tools/build-debs.sh runs this at build time; nothing generated is
#       committed.
#
# Usage: tools/gen-desktop-depends.sh [--list]
#   (default)  one line for the substvar:  pkg-a, pkg-b, pkg-c
#   --list     one package per line, with the list it came from (for review)
#   -h, --help show this help
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

main() {
	local mode="line" dir list pkg own parsed
	local -a lists pkgs
	local -A seen=() is_own=()

	while (($#)); do
		case "$1" in
			--list)    mode="list"; shift ;;
			-h|--help) usage; exit 0 ;;
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
		if nimbus_list_excluded_from_desktop "$list"; then
			continue
		fi
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
		done
	done
	((${#seen[@]})) || die "The package lists produced no dependencies."

	if [[ "$mode" == list ]]; then
		for pkg in "${!seen[@]}"; do
			printf '%-40s %s\n' "$pkg" "${seen[$pkg]}"
		done | LC_ALL=C sort
	else
		printf '%s\n' "${!seen[@]}" | LC_ALL=C sort | paste -sd ',' - | sed 's/,/, /g'
	fi
}

main "$@"
