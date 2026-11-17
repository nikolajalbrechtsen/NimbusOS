#!/bin/bash
# =============================================================================
# tools/deb-version.sh — the NimbusOS version, derived from git
# =============================================================================
# What: turns the git state into the Debian version, APT suite, channel and
#       display version shared by all six packages and the ISO.
#
# Why:  "the version comes from the git tag" (spec 4.1). Release tags map as
#           vX.Y.Z         ->  X.Y.Z          suite stable, channel stable
#           vX.Y.Z-beta.N  ->  X.Y.Z~beta.N   suite beta,   channel beta
#       so a final release always sorts above its betas (docs/PLAN.md
#       NOTE-32). Any other commit, or a modified working tree, gets a
#       development version that sorts after its newest ancestor release:
#           X.Y.Z+gitYYYYMMDD.N.gSHA[.dirty]   suite UNRELEASED, channel dev
#       Development versions are never published.
#
# Usage: tools/deb-version.sh [--tag TAG] [--env | --field NAME]
#   --tag TAG     use release tag TAG instead of looking at HEAD. CI passes
#                 GITHUB_REF_NAME. If TAG exists locally it must point at HEAD.
#   --env         print shell assignments, for use with eval (the default)
#   --field NAME  print one value: version, display, suite, channel, tag,
#                 commit, epoch
#   -h, --help    show this help
#
# Values:
#   NIMBUS_VERSION          Debian version            0.2.1~beta.3
#   NIMBUS_DISPLAY_VERSION  version shown to people   0.2.1-beta.3
#   NIMBUS_SUITE            stable | beta | UNRELEASED (debian/changelog)
#   NIMBUS_CHANNEL          stable | beta | dev        (os-release, UI)
#   NIMBUS_TAG              release tag, empty for development builds
#   NIMBUS_GIT_COMMIT       abbreviated commit id
#   SOURCE_DATE_EPOCH       commit time, for reproducible builds
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# Release tags: vMAJOR.MINOR.PATCH with an optional -beta.N, no leading zeros.
TAG_RE='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-beta\.(0|[1-9][0-9]*))?$'

# tag_to_debian TAG — print the Debian version for a release tag, or fail.
tag_to_debian() {
	[[ "$1" =~ $TAG_RE ]] || return 1
	local v="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
	if [[ -n "${BASH_REMATCH[5]}" ]]; then
		v+="~beta.${BASH_REMATCH[5]}"
	fi
	printf '%s\n' "$v"
}

# highest_release_tag GIT-TAG-ARGS... — among the tags git lists, print the
# release tag with the highest Debian version (empty if none).
highest_release_tag() {
	local t v best="" best_v=""
	while IFS= read -r t; do
		v="$(tag_to_debian "$t")" || continue
		if [[ -z "$best" ]] || dpkg --compare-versions "$v" gt "$best_v"; then
			best="$t"
			best_v="$v"
		fi
	done < <(nimbus_git tag "$@" --list 'v[0-9]*')
	printf '%s\n' "$best"
}

main() {
	local opt_tag="" mode="env" field="" in_git="" dirty=""
	local version display suite channel tag commit epoch base_tag base count date

	while (($#)); do
		case "$1" in
			--tag)   [[ $# -ge 2 ]] || die "--tag needs a value"; opt_tag="$2"; shift 2 ;;
			--env)   mode="env"; shift ;;
			--field) [[ $# -ge 2 ]] || die "--field needs a value"; mode="field"; field="$2"; shift 2 ;;
			-h|--help) usage; exit 0 ;;
			*) die "Unknown option: $1 (see --help)" ;;
		esac
	done

	command -v dpkg >/dev/null 2>&1 || die "dpkg is required (for version comparison)."

	if nimbus_git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		in_git=1
		commit="$(nimbus_git rev-parse --short=7 HEAD)"
		epoch="$(nimbus_git log -1 --format=%ct HEAD)"
		if [[ -n "$(nimbus_git status --porcelain --untracked-files=no)" ]]; then
			dirty=1
		fi
	else
		commit="unknown"
		epoch="${SOURCE_DATE_EPOCH:-$(date -u +%s)}"
	fi

	# 1. Which release tag, if any, describes this build?
	tag=""
	if [[ -n "$opt_tag" ]]; then
		tag_to_debian "$opt_tag" >/dev/null \
			|| die "'$opt_tag' is not a release tag. Use vX.Y.Z or vX.Y.Z-beta.N."
		if [[ -n "$in_git" ]] && nimbus_git rev-parse -q --verify "refs/tags/$opt_tag" >/dev/null; then
			[[ "$(nimbus_git rev-parse "refs/tags/$opt_tag^{commit}")" == "$(nimbus_git rev-parse HEAD)" ]] \
				|| die "Tag $opt_tag does not point at the checked-out commit $commit. Check out the tag first."
		fi
		tag="$opt_tag"
	elif [[ -n "$in_git" ]]; then
		tag="$(highest_release_tag --points-at HEAD)"
	else
		die "This is not a git checkout and no --tag was given, so the version is unknown."
	fi
	if [[ -n "$tag" && -n "$dirty" ]]; then
		[[ -z "$opt_tag" ]] || die "The working tree has uncommitted changes; refusing to label it as release $opt_tag."
		warn "HEAD is tagged $tag but the working tree has uncommitted changes; building a development version."
		tag=""
	fi

	# 2. Release build, or development build?
	if [[ -n "$tag" ]]; then
		version="$(tag_to_debian "$tag")"
		display="${tag#v}"
		if [[ "$tag" == *-beta.* ]]; then
			suite="beta"
			channel="beta"
		else
			suite="stable"
			channel="stable"
		fi
	else
		base_tag="$(highest_release_tag --merged HEAD)"
		if [[ -n "$base_tag" ]]; then
			base="$(tag_to_debian "$base_tag")"
			count="$(nimbus_git rev-list --count "${base_tag}..HEAD")"
		else
			base="0.0.0"
			count="$(nimbus_git rev-list --count HEAD)"
		fi
		date="$(TZ=UTC nimbus_git log -1 --date=format-local:%Y%m%d --format=%cd HEAD)"
		version="${base}+git${date}.${count}.g${commit}"
		if [[ -n "$dirty" ]]; then
			version+=".dirty"
		fi
		display="$version"
		suite="UNRELEASED"
		channel="dev"
	fi

	# 3. Output.
	case "$mode" in
		env)
			printf 'NIMBUS_VERSION=%q\n' "$version"
			printf 'NIMBUS_DISPLAY_VERSION=%q\n' "$display"
			printf 'NIMBUS_SUITE=%q\n' "$suite"
			printf 'NIMBUS_CHANNEL=%q\n' "$channel"
			printf 'NIMBUS_TAG=%q\n' "$tag"
			printf 'NIMBUS_GIT_COMMIT=%q\n' "$commit"
			printf 'SOURCE_DATE_EPOCH=%q\n' "$epoch"
			;;
		field)
			case "$field" in
				version) printf '%s\n' "$version" ;;
				display) printf '%s\n' "$display" ;;
				suite)   printf '%s\n' "$suite" ;;
				channel) printf '%s\n' "$channel" ;;
				tag)     printf '%s\n' "$tag" ;;
				commit)  printf '%s\n' "$commit" ;;
				epoch)   printf '%s\n' "$epoch" ;;
				*) die "Unknown field '$field'. Use version, display, suite, channel, tag, commit or epoch." ;;
			esac
			;;
	esac
}

main "$@"
