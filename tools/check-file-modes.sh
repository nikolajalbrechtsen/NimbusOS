#!/bin/bash
# =============================================================================
# tools/check-file-modes.sh — the file modes recorded in git must match each file's role
# =============================================================================
# What: compares every tracked file's mode in git's index (git ls-files -s)
#       with tools/file-modes.conf. Files listed there must be 100755,
#       everything else 100644, and no symbolic links. An executable must
#       also start with "#!", or it couldn't run. Prints the exact
#       `git update-index --chmod` command for every mismatch.
# Why:  builds use git's modes (build/lib/common.sh nimbus_apply_git_modes),
#       and a Windows checkout can't show them, so CI checks them instead.
#
# Usage: tools/check-file-modes.sh      (exit 0 = all modes correct)
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
CONF="$ROOT/tools/file-modes.conf"
git_() { git -c safe.directory="$ROOT" -C "$ROOT" "$@"; }

mapfile -t globs < <(sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e '/^$/d' "$CONF")
((${#globs[@]})) || { echo "check-file-modes: no globs in $CONF" >&2; exit 2; }

is_executable_role() {
	local path="$1" glob
	for glob in "${globs[@]}"; do
		# shellcheck disable=SC2053  # the right-hand side is a glob on purpose
		[[ "$path" == $glob ]] && return 0
	done
	return 1
}

problems=0
checked=0
while IFS= read -r -d '' entry; do
	mode="${entry%% *}"
	path="${entry#*$'\t'}"
	checked=$((checked + 1))
	if [[ "$mode" == 120000 ]]; then
		echo "symlink not allowed: $path"
		problems=$((problems + 1))
		continue
	fi
	if is_executable_role "$path"; then
		if [[ "$mode" != 100755 ]]; then
			echo "must be executable: $path   ->  git update-index --chmod=+x '$path'"
			problems=$((problems + 1))
		elif [[ -f "$ROOT/$path" && "$(head -c 2 -- "$ROOT/$path")" != "#!" ]]; then
			echo "executable without a #! line: $path"
			problems=$((problems + 1))
		fi
	elif [[ "$mode" != 100644 ]]; then
		echo "must not be executable: $path   ->  git update-index --chmod=-x '$path'"
		problems=$((problems + 1))
	fi
done < <(git_ ls-files -s -z)

if ((problems)); then
	echo "check-file-modes: $problems problem(s) in $checked tracked files (rules: tools/file-modes.conf)" >&2
	exit 1
fi
echo "check-file-modes: all $checked tracked files have the mode their role requires"
