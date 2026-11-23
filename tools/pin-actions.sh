#!/bin/bash
# =============================================================================
# tools/pin-actions.sh: pin the workflows' actions to commit SHAs
# =============================================================================
# What: for every `uses: OWNER/REPO@REF` line in .github/workflows/*.yml:
#         default   pin it to the commit its tag points to, written as
#                   `uses: OWNER/REPO@<40-hex SHA> # vX.Y.Z`. The tag comes
#                   from the trailing comment, or from REF when REF is a tag
#                   (how a new action is added: `@v7.0.1`).
#         --latest  first move each tag to that action's latest release
#                   (GitHub's releases/latest), then pin.
#         --check   change nothing; fail if a line isn't pinned to a full SHA
#                   with a tag comment, or the SHA isn't that tag's commit.
#       Tags are resolved with `git ls-remote` (an annotated tag's peeled
#       commit, ^{}, wins). Local actions (./…) and docker:// are left alone.
# Why:  docs/PLAN.md NOTE-42 and the review: third-party code runs only at a
#       reviewed commit; .github/dependabot.yml moves the pins and their
#       comments forward in pull requests.
#
# Usage: tools/pin-actions.sh [--latest | --check]
#        Network (github.com, api.github.com). GITHUB_TOKEN is used for the
#        API if set. Needs git, curl and python3.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

mode=pin
case "${1:-}" in
	"") ;;
	--latest) mode=latest ;;
	--check) mode=check ;;
	-h|--help) sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
	*) die "Unknown option: $1 (see --help)" ;;
esac

declare -A commit_of=() latest_of=()
resolve() {   # resolve REPO TAG: print the commit TAG points to
	local key="$1@$2" lines
	if [[ -z "${commit_of[$key]:-}" ]]; then
		lines="$(git ls-remote --tags "https://github.com/$1" "refs/tags/$2" "refs/tags/$2^{}")" \
			|| die "Can't reach https://github.com/$1."
		commit_of[$key]="$(awk -v t="refs/tags/$2" '$2 == t "^{}" { peeled = $1 } $2 == t { plain = $1 }
			END { print (peeled != "" ? peeled : plain) }' <<<"$lines")"
		[[ "${commit_of[$key]}" =~ ^[0-9a-f]{40}$ ]] || die "$1 has no tag $2."
	fi
	printf '%s\n' "${commit_of[$key]}"
}
latest_release() {   # latest_release REPO: the tag of its latest release
	if [[ -z "${latest_of[$1]:-}" ]]; then
		local -a auth=()
		[[ -z "${GITHUB_TOKEN:-}" ]] || auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
		latest_of[$1]="$(curl -fsSL "${auth[@]}" -H "Accept: application/vnd.github+json" \
			"https://api.github.com/repos/$1/releases/latest" | python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])')" \
			|| die "Can't read the latest release of $1."
	fi
	printf '%s\n' "${latest_of[$1]}"
}

USES_RE='^([[:space:]]*-?[[:space:]]*uses:[[:space:]]*)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(/[^@[:space:]]*)?@([^[:space:]#]+)[[:space:]]*(#[[:space:]]*([^[:space:]]+))?[[:space:]]*$'
bad_refs=0
changed=0
shopt -s nullglob
for file in "$NIMBUS_REPO_ROOT"/.github/workflows/*.yml; do
	out="$(mktemp)"
	while IFS= read -r line || [[ -n "$line" ]]; do
		if [[ "$line" =~ $USES_RE ]]; then
			prefix="${BASH_REMATCH[1]}" repo="${BASH_REMATCH[2]}" sub="${BASH_REMATCH[3]}" ref="${BASH_REMATCH[4]}" tag="${BASH_REMATCH[6]}"
			[[ -n "$tag" ]] || tag="$ref"
			if [[ "$mode" == check ]]; then
				if [[ ! "$ref" =~ ^[0-9a-f]{40}$ || "$tag" == "$ref" ]]; then
					warn "${file##*/}: $repo$sub@$ref is not pinned to a commit SHA with a tag comment."
					bad_refs=$((bad_refs + 1))
				elif [[ "$(resolve "$repo" "$tag")" != "$ref" ]]; then
					warn "${file##*/}: $repo$sub@$ref is not the commit of $tag ($(resolve "$repo" "$tag"))."
					bad_refs=$((bad_refs + 1))
				fi
				printf '%s\n' "$line" >>"$out"
				continue
			fi
			[[ "$mode" != latest ]] || tag="$(latest_release "$repo")"
			new="${prefix}${repo}${sub}@$(resolve "$repo" "$tag") # $tag"
			[[ "$new" == "$line" ]] || { log "${file##*/}: $repo$sub -> $tag"; changed=$((changed + 1)); }
			printf '%s\n' "$new" >>"$out"
		else
			printf '%s\n' "$line" >>"$out"
		fi
	done <"$file"
	if [[ "$mode" == check ]]; then rm -f "$out"; else cat "$out" >"$file"; rm -f "$out"; fi
done

if [[ "$mode" == check ]]; then
	((bad_refs == 0)) || die "$bad_refs action reference(s) are not pinned correctly. Run tools/pin-actions.sh."
	log "Every action is pinned to its tag's commit."
else
	log "$changed reference(s) changed."
fi
