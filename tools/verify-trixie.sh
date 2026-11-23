#!/bin/bash
# =============================================================================
# tools/verify-trixie.sh — prove that every package and live-build flag NimbusOS uses exists in Debian 13
# =============================================================================
# What: checks NimbusOS's inputs against the real Trixie archive
#       (NIMBUS_BUILD_MIRROR, deb.debian.org by default) through a private
#       APT configuration that never touches the host's own APT setup:
#         1. every list in build/config/package-lists/ follows live-build's
#            format rules (one package per line, no characters that
#            live-build's printf/read handling would mangle);
#         2. every listed package has an amd64 candidate in trixie,
#            trixie-updates or trixie-security (main contrib non-free
#            non-free-firmware), or in Docker's repository;
#         3. all *.list.chroot packages install together from scratch with
#            Recommends, as live-build installs them (apt-get --simulate
#            against an empty dpkg database);
#         4. each *.list.binary resolves on its own, as live-build's
#            binary_package-lists does;
#         5. the build and runtime dependencies in packages/*/debian/control
#            exist (packages not written yet are skipped and reported);
#         6. live-build accepts every flag in build/lib/common.sh: `lb config`
#            runs with exactly that flag list, and a deliberately bogus flag
#            must be rejected, which proves the check can fail.
#       It ends with a short report on a watch list of packages and flags.
#
# Why:  ground rule 3 (verify, don't invent). CI runs this on every push.
#       ruff and other CI-only tools are deliberately not checked here.
#
# Usage: sudo tools/verify-trixie.sh [options]
#   --skip-docker   leave out Docker's repository and 50-docker.list.chroot
#                   (for runs before Docker's key is committed, deliverable 4)
#   --watch "P ..." packages to report on (default: NIMBUS_VERIFY_WATCH)
#   -h, --help      show this help
#   Needs Debian 13 as root (it installs live-build if missing) and network
#   access to the mirrors. tools/dev-shell.sh gives you that on any host.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# Packages the report always shows (wsdd is listed on purpose: it is the
# spec's name, and the report shows why wsdd2 is used instead).
NIMBUS_VERIFY_WATCH="${NIMBUS_VERIFY_WATCH:-snapraid wsdd wsdd2 samba-vfs-modules fonts-inter}"

opt_skip_docker=""
opt_watch=""
apt_dir=""
problems=()

aptx() {
	APT_CONFIG="$apt_dir/apt.conf" "$@"
}

# candidate PKG — candidate version in the private APT configuration, or "".
candidate() {
	local c
	c="$(aptx apt-cache policy -- "$1" 2>/dev/null | awk '/^  Candidate:/ { print $2; exit }')"
	[[ "$c" == "(none)" ]] && c=""
	printf '%s' "$c"
}

# provided NAME [VERSION] — true if a real package provides the virtual package
# NAME (with "= VERSION" when VERSION is given), as debhelper provides
# "debhelper-compat (= 13)". apt-cache policy has no candidate for those.
provided() {
	aptx apt-cache showpkg -- "$1" 2>/dev/null | awk -v want="${2:+(= $2)}" '
		/^Reverse Provides:/ { in_rp = 1; next }
		in_rp && NF >= 2 { if (want == "" || index($0, want)) found = 1 }
		END { exit !found }'
}

# component PKG — archive area of the candidate (main, non-free-firmware, ...).
component() {
	aptx apt-cache show --no-all-versions -- "$1" 2>/dev/null \
		| awk '/^Filename: / { n = split($2, p, "/"); if (p[2] == "updates") print p[3] " (security)"; else print p[2]; exit }'
}

# control_relations FILE — one dependency relation group per line from the
# Build-Depends*, Pre-Depends, Depends and Recommends fields of a
# debian/control file.
control_relations() {
	awk '
		function flush() { if (keep) print value; keep = 0; value = "" }
		/^#/ { next }
		/^[^ \t][^:]*:/ {
			flush()
			name = substr($0, 1, index($0, ":") - 1)
			if (name ~ /^(Build-Depends|Build-Depends-Indep|Build-Depends-Arch|Pre-Depends|Depends|Recommends)$/) {
				keep = 1
				value = substr($0, index($0, ":") + 1)
			}
			next
		}
		/^[ \t]/ { if (keep) value = value " " $0; next }
		/^$/ { flush(); next }
		END { flush() }
	' "$1" | tr ',' '\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$' || true
}

main() {
	local dir list f pkg parsed cand comp log lbdir group alt exact ok ctrl n line
	local -a chroot_lists binary_lists pkgs chroot_pkgs=() all_pkgs=() controls watch alts
	local -A from=() is_own=() binary_of=()

	while (($#)); do
		case "$1" in
			--skip-docker) opt_skip_docker=1; shift ;;
			--watch)       [[ $# -ge 2 ]] || die "--watch needs a value"; opt_watch="$2"; shift 2 ;;
			-h|--help)     usage; exit 0 ;;
			*) die "Unknown option: $1 (see --help)" ;;
		esac
	done

	nimbus_load_conf
	nimbus_check_host
	nimbus_apt_install_missing live-build gpg ca-certificates
	export LC_ALL=C.UTF-8
	for pkg in "${NIMBUS_PACKAGES[@]}"; do
		is_own["$pkg"]=1
	done

	# --- 1. List format ------------------------------------------------------
	step "1. Package-list format"
	dir="$NIMBUS_REPO_ROOT/build/config/package-lists"
	for f in "$dir"/*; do
		case "$f" in
			*.list.chroot|*.list.binary) ;;
			*) die "$f: only *.list.chroot and *.list.binary are used (a plain *.list would go into both the image and the ISO pool)." ;;
		esac
	done
	mapfile -t chroot_lists < <(find "$dir" -maxdepth 1 -type f -name '*.list.chroot' | LC_ALL=C sort)
	mapfile -t binary_lists < <(find "$dir" -maxdepth 1 -type f -name '*.list.binary' | LC_ALL=C sort)
	for list in "${chroot_lists[@]}" "${binary_lists[@]}"; do
		parsed="$(nimbus_parse_package_list "$list")"
		[[ -n "$parsed" ]] || continue
		mapfile -t pkgs <<<"$parsed"
		for pkg in "${pkgs[@]}"; do
			[[ -z "${is_own[$pkg]:-}" ]] || continue
			if [[ -n "$opt_skip_docker" && "$(basename "$list")" == 50-docker.list.chroot ]]; then
				continue
			fi
			if [[ "$list" == *.list.chroot ]]; then
				chroot_pkgs+=("$pkg")
			else
				binary_of["$(basename "$list")"]+="$pkg "
			fi
			if [[ -z "${from[$pkg]:-}" ]]; then
				all_pkgs+=("$pkg")
				from["$pkg"]="$(basename "$list")"
			else
				from["$pkg"]+=", $(basename "$list")"
			fi
		done
	done
	log "${#chroot_lists[@]} chroot lists, ${#binary_lists[@]} binary lists, ${#all_pkgs[@]} distinct packages: format OK."
	# The metapackage's Depends and Recommends lines must be producible, too.
	"$NIMBUS_REPO_ROOT/tools/gen-desktop-depends.sh" --depends >/dev/null
	"$NIMBUS_REPO_ROOT/tools/gen-desktop-depends.sh" --recommends >/dev/null

	# --- 2. Private APT configuration ------------------------------------------
	step "2. Trixie package indices (${NIMBUS_BUILD_MIRROR})"
	nimbus_mktemp_dir apt_dir
	nimbus_apt_private_init "$apt_dir"
	cat >"$apt_dir/etc/sources.list.d/debian.sources" <<EOF
Types: deb
URIs: ${NIMBUS_BUILD_MIRROR}
Suites: ${NIMBUS_DEBIAN_SUITE} ${NIMBUS_DEBIAN_SUITE}-updates
Components: ${NIMBUS_ARCHIVE_AREAS}
Signed-By: $(nimbus_debian_keyring)

Types: deb
URIs: ${NIMBUS_BUILD_MIRROR_SECURITY}
Suites: ${NIMBUS_DEBIAN_SUITE}-security
Components: ${NIMBUS_ARCHIVE_AREAS}
Signed-By: $(nimbus_debian_keyring)
EOF
	if [[ -n "$opt_skip_docker" ]]; then
		warn "Skipping Docker's repository and 50-docker.list.chroot (--skip-docker)."
	else
		nimbus_check_docker_key
		nimbus_dearmor_into "$apt_dir/docker.gpg" "$(nimbus_path "$NIMBUS_DOCKER_KEY_FILE")"
		cat >"$apt_dir/etc/sources.list.d/docker.sources" <<EOF
Types: deb
URIs: ${NIMBUS_DOCKER_APT_URL}
Suites: ${NIMBUS_DEBIAN_SUITE}
Components: stable
Signed-By: ${apt_dir}/docker.gpg
EOF
	fi
	aptx apt-get -q update

	# --- 3. Every listed package exists ----------------------------------------
	step "3. Every listed package has a candidate"
	for pkg in "${all_pkgs[@]}"; do
		cand="$(candidate "$pkg")"
		if [[ -z "$cand" ]]; then
			problems+=("$pkg (${from[$pkg]}) does not exist in ${NIMBUS_DEBIAN_SUITE} for ${NIMBUS_ARCH}")
		fi
	done
	log "Checked ${#all_pkgs[@]} packages."

	# --- 4. Chroot lists install together ----------------------------------------
	step "4. All *.list.chroot packages install together (with Recommends)"
	log="$apt_dir/simulate-chroot.log"
	if aptx apt-get install --simulate -q -o APT::Install-Recommends=true -- "${chroot_pkgs[@]}" >"$log" 2>&1; then
		log "Resolves: $(grep -c '^Inst ' "$log") packages would be installed."
	else
		problems+=("the *.list.chroot packages cannot be installed together: $(grep -E '^(E:| )' "$log" | head -n 15 | tr '\n' ' ')")
	fi

	# --- 5. Binary lists resolve one by one -------------------------------------
	step "5. Each *.list.binary resolves on its own"
	for list in "${!binary_of[@]}"; do
		read -r -a pkgs <<<"${binary_of[$list]}"
		log="$apt_dir/simulate-$list.log"
		if aptx apt-get install --simulate -q -o APT::Install-Recommends=false -- "${pkgs[@]}" >"$log" 2>&1; then
			log "$list: resolves ($(grep -c '^Inst ' "$log") packages in the ISO pool)."
		else
			problems+=("$list does not resolve: $(grep -E '^(E:| )' "$log" | head -n 10 | tr '\n' ' ')")
		fi
	done

	# --- 6. Dependencies of the NimbusOS packages -------------------------------
	step "6. Dependencies named in packages/*/debian/control"
	controls=()
	if [[ -d "$NIMBUS_REPO_ROOT/packages" ]]; then
		mapfile -t controls < <(find "$NIMBUS_REPO_ROOT/packages" -mindepth 3 -maxdepth 3 -path '*/debian/control' | LC_ALL=C sort)
	fi
	for pkg in "${NIMBUS_PACKAGES[@]}"; do
		[[ -f "$NIMBUS_REPO_ROOT/packages/$pkg/debian/control" ]] \
			|| log "packages/$pkg/debian/control is not written yet (deliverable 5); skipped."
	done
	n=0
	for ctrl in "${controls[@]}"; do
		while IFS= read -r group; do
			[[ "$group" == *'${'* ]] && continue
			IFS='|' read -r -a alts <<<"$group"
			ok=""
			for alt in "${alts[@]}"; do
				exact="$(sed -nE 's/.*\([[:space:]]*=[[:space:]]*([^)[:space:]]+)[[:space:]]*\).*/\1/p' <<<"$alt")"
				alt="$(sed -E -e 's/\([^)]*\)//g' -e 's/\[[^]]*\]//g' -e 's/<[^>]*>//g' -e 's/:(any|native)//' -e 's/[[:space:]]//g' <<<"$alt")"
				[[ -n "$alt" ]] || continue
				if [[ -n "${is_own[$alt]:-}" || -n "$(candidate "$alt")" ]] || provided "$alt" "$exact"; then
					ok=1
					break
				fi
			done
			n=$((n + 1))
			[[ -n "$ok" ]] || problems+=("${ctrl#"$NIMBUS_REPO_ROOT/"}: no installable package for \"$group\"")
		done < <(control_relations "$ctrl")
	done
	log "Checked $n dependency relations in ${#controls[@]} debian/control files."

	# --- 7. live-build flags --------------------------------------------------
	step "7. live-build accepts every lb config flag (live-build $(dpkg-query -W -f='${Version}' live-build))"
	nimbus_lb_config_args "verify"
	nimbus_mktemp_dir lbdir
	log="$apt_dir/lb-config.log"
	if (cd "$lbdir" && lb config "${NIMBUS_LB_ARGS[@]}") >"$log" 2>&1; then
		log "lb config accepted all ${#NIMBUS_LB_ARGS[@]} arguments."
	else
		problems+=("lb config rejected the flag list: $(grep -iE 'unrecogni|unknown|invalid|error' "$log" | head -n 5 | tr '\n' ' ')")
	fi
	nimbus_mktemp_dir f
	if (cd "$f" && lb config --nimbus-no-such-option) >/dev/null 2>&1; then
		problems+=("lb config accepted a bogus option, so step 7 proves nothing; check how this live-build version reports errors")
	else
		log "Control: lb config rejects a bogus option, so a typo in the flag list would be caught."
	fi

	# --- Report ---------------------------------------------------------------
	step "Report"
	read -r -a watch <<<"${opt_watch:-$NIMBUS_VERIFY_WATCH}"
	printf '  %-22s %-30s %-24s %s\n' PACKAGE CANDIDATE COMPONENT "IN NIMBUSOS LISTS" >&2
	for pkg in "${watch[@]}"; do
		cand="$(candidate "$pkg")"
		comp=""
		[[ -n "$cand" ]] && comp="$(component "$pkg")"
		printf '  %-22s %-30s %-24s %s\n' "$pkg" "${cand:-MISSING}" "${comp:--}" "${from[$pkg]:-no}" >&2
	done
	printf '\n  %-40s %s\n' "LB CONFIG SETTING" "STORED VALUE" >&2
	while IFS= read -r line; do
		printf '  %-40s %s\n' "${line%%=*}" "${line#*=}" >&2
	done < <(grep -rhE '^LB_(CHROOT_SQUASHFS_COMPRESSION_TYPE|UEFI_SECURE_BOOT|BOOTLOADER_BIOS|BOOTLOADER_EFI|ARCHIVE_AREAS)=' "$lbdir/config" 2>/dev/null | LC_ALL=C sort)

	if ((${#problems[@]})); then
		printf '\n[%s] FAILED: %d problem(s):\n' "$_nimbus_prog" "${#problems[@]}" >&2
		printf '  - %s\n' "${problems[@]}" >&2
		exit 1
	fi
	printf '\n[%s] PASSED: every package and flag checked exists in %s.\n' "$_nimbus_prog" "$NIMBUS_DEBIAN_SUITE" >&2
}

main "$@"
