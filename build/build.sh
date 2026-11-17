#!/bin/bash
# =============================================================================
# build/build.sh — build the NimbusOS live/installer ISO
# =============================================================================
# What: 1. checks the build host (root, Debian 13, amd64, free space) and
#          installs live-build, debootstrap, devscripts and the other host
#          tools if they are missing;
#       2. checks Docker's key fingerprint and that the NimbusOS APT
#          repository answers with a correctly signed, unexpired InRelease
#          (docs/PLAN.md NOTE-04);
#       3. gets the six NimbusOS .deb files: builds them with
#          tools/build-debs.sh, takes them from --debs-dir, or downloads them
#          from the published repository with --from-repo; then checks they
#          share one version and carry the committed archive key;
#       4. prepares build/work/: a fresh copy of build/config/ with
#          @NIMBUS_*@ tokens filled in, the .debs in config/packages.chroot/,
#          bootloader overlays merged onto live-build's own templates, the
#          GRUB theme taken from nimbusos-branding, splash images rendered;
#       5. runs `lb clean`, `lb config` (flags explained in
#          build/lib/common.sh) and `lb build`;
#       6. writes dist/nimbusos-amd64.iso and dist/nimbusos-amd64.iso.sha256.
#
# Why:  one reproducible, re-runnable entry point, used locally and by
#       release.yml, so the ISO contains exactly the packages the APT
#       repository serves (ground rule 2). The checked-in tree is never
#       modified; all generated files live in build/work/ and dist/.
#
# Usage: sudo ./build/build.sh [options]
#   --debs-dir DIR      use the six .deb files in DIR (release.yml passes the
#                       packages it has just published)
#   --from-repo         download the .deb files from the published APT
#                       repository instead of building them
#   --suite SUITE       suite for --from-repo: stable (default) or beta
#   --version VERSION   exact Debian version for --from-repo (default: newest)
#   --tag TAG           build the packages for release tag TAG
#   --output DIR        output directory (default: NIMBUS_OUTPUT_DIR = dist/)
#   --skip-repo-check   do not check the APT repository first (not recommended:
#                       the build then fails late if it is unreachable)
#   --clean-cache       also delete live-build's package cache
#   -h, --help          show this help
# Everything else is set in build/nimbus.conf.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# Everything else/p' "$0" | sed 's/^# \{0,1\}//'
}

opt_debs_dir=""
opt_from_repo=""
opt_suite=""
opt_version=""
opt_tag=""
opt_output=""
opt_skip_repo_check=""
opt_clean_cache=""

work=""
out=""
debs=""

# -----------------------------------------------------------------------------
# --from-repo: download the packages through a private, signature-checked APT
# configuration (the host's own APT setup is neither used nor changed).
# -----------------------------------------------------------------------------
fetch_from_repo() {
	local dest="$1" tmp ver pkg
	local -a keys pkgs=()
	nimbus_mktemp_dir tmp

	mapfile -t keys < <(nimbus_archive_key_files)
	((${#keys[@]})) || die "No NimbusOS public key is committed in $NIMBUS_ARCHIVE_KEYS_DIR/; cannot verify downloads (docs/SIGNING.md)."
	nimbus_dearmor_into "$tmp/nimbusos-archive.gpg" "${keys[@]}"

	mkdir -p "$tmp/etc/apt.conf.d" "$tmp/etc/sources.list.d" "$tmp/etc/preferences.d" \
		"$tmp/etc/trusted.gpg.d" "$tmp/state/lists/partial" "$tmp/cache/archives/partial"
	: >"$tmp/etc/sources.list"
	: >"$tmp/status"
	cat >"$tmp/etc/sources.list.d/nimbusos.sources" <<EOF
Types: deb
URIs: ${NIMBUS_APT_URL}/
Suites: ${opt_suite}
Components: ${NIMBUS_APT_COMPONENT}
Signed-By: ${tmp}/nimbusos-archive.gpg
EOF
	cat >"$tmp/apt.conf" <<EOF
// Private APT configuration for build.sh --from-repo.
Dir::Etc "${tmp}/etc";
Dir::State "${tmp}/state";
Dir::State::status "${tmp}/status";
Dir::Cache "${tmp}/cache";
APT::Architecture "${NIMBUS_ARCH}";
APT::Architectures { "${NIMBUS_ARCH}"; };
APT::Sandbox::User "root";
Acquire::Retries "3";
EOF

	log "Downloading index of suite '$opt_suite' from $NIMBUS_APT_URL"
	APT_CONFIG="$tmp/apt.conf" apt-get -q update

	if [[ -n "$opt_version" ]]; then
		ver="$opt_version"
	else
		ver="$(APT_CONFIG="$tmp/apt.conf" apt-cache show --no-all-versions nimbusos-desktop | sed -n 's/^Version: //p')"
		ver="${ver%%$'\n'*}"
		[[ -n "$ver" ]] || die "nimbusos-desktop is not available in suite '$opt_suite' of $NIMBUS_APT_URL."
	fi
	for pkg in "${NIMBUS_PACKAGES[@]}"; do
		pkgs+=("${pkg}=${ver}")
	done
	log "Downloading ${NIMBUS_NAME} $ver"
	(cd "$dest" && APT_CONFIG="$tmp/apt.conf" apt-get -q download "${pkgs[@]}")
}

# -----------------------------------------------------------------------------
# Bootloader overlays (docs/PLAN.md §1): live-build uses config/bootloaders/X
# INSTEAD of its own template X, so each overlay is merged onto a copy of the
# shipped template. Symbolic links in the templates are dereferenced, because
# they point at sibling template directories that are not copied.
# -----------------------------------------------------------------------------
prepare_bootloaders() {
	local dir="$work/config/bootloaders" overlay name defaults merged extract theme svg
	local -a branding svgs
	[[ -d "$dir" ]] || return 0

	for overlay in "$dir"/*/; do
		overlay="${overlay%/}"
		name="$(basename "$overlay")"
		defaults="/usr/share/live/build/bootloaders/$name"
		if [[ -d "$defaults" ]]; then
			nimbus_mktemp_dir merged
			cp -RL --preserve=mode,timestamps -- "$defaults/." "$merged/"
			cp -R --preserve=mode,timestamps -- "$overlay/." "$merged/"
			rm -rf -- "$overlay"
			mkdir -p -- "$overlay"
			cp -R --preserve=mode,timestamps -- "$merged/." "$overlay/"
			log "bootloaders/$name: live-build template + NimbusOS overlay"
		else
			warn "live-build has no template for bootloaders/$name; using the NimbusOS files alone."
		fi
	done

	# The live GRUB menu uses the same theme as installed systems.
	if [[ -d "$dir/grub-pc" ]]; then
		mapfile -t branding < <(find "$debs" -maxdepth 1 -name 'nimbusos-branding_*.deb')
		nimbus_mktemp_dir extract
		dpkg-deb -x "${branding[0]}" "$extract"
		theme="$extract/usr/share/grub/themes/$NIMBUS_ID"
		[[ -f "$theme/theme.txt" ]] || die "nimbusos-branding does not contain /usr/share/grub/themes/$NIMBUS_ID/theme.txt."
		rm -rf -- "$dir/grub-pc/live-theme"
		mkdir -p -- "$dir/grub-pc/live-theme"
		cp -R --preserve=mode,timestamps -- "$theme/." "$dir/grub-pc/live-theme/"
		log "bootloaders/grub-pc: live-theme taken from $(basename "${branding[0]}")"
	fi

	# Splash screens are kept as SVG in the repository (no binary assets) and
	# rendered here; live-build's own splash template is dropped so it can't
	# replace ours.
	mapfile -t svgs < <(find "$dir" -type f -name 'splash.svg')
	for svg in "${svgs[@]}"; do
		rsvg-convert --format png --output "${svg%.svg}.png" -- "$svg"
		rm -f -- "$svg" "$(dirname "$svg")/splash.svg.in"
		log "Rendered ${svg#"$work/config/"} to PNG"
	done
}

main() {
	local version display free iso_src iso manifest size limit pkg
	local -a build_args=()

	while (($#)); do
		case "$1" in
			--debs-dir)        [[ $# -ge 2 ]] || die "--debs-dir needs a value"; opt_debs_dir="$2"; shift 2 ;;
			--from-repo)       opt_from_repo=1; shift ;;
			--suite)           [[ $# -ge 2 ]] || die "--suite needs a value"; opt_suite="$2"; shift 2 ;;
			--version)         [[ $# -ge 2 ]] || die "--version needs a value"; opt_version="$2"; shift 2 ;;
			--tag)             [[ $# -ge 2 ]] || die "--tag needs a value"; opt_tag="$2"; shift 2 ;;
			--output)          [[ $# -ge 2 ]] || die "--output needs a value"; opt_output="$2"; shift 2 ;;
			--skip-repo-check) opt_skip_repo_check=1; shift ;;
			--clean-cache)     opt_clean_cache=1; shift ;;
			-h|--help)         usage; exit 0 ;;
			*) die "Unknown option: $1 (see --help)" ;;
		esac
	done
	if [[ -n "$opt_debs_dir" && -n "$opt_from_repo" ]]; then
		die "--debs-dir and --from-repo cannot be combined."
	fi
	if [[ -z "$opt_from_repo" && ( -n "$opt_suite" || -n "$opt_version" ) ]]; then
		die "--suite and --version only apply to --from-repo."
	fi
	if [[ -n "$opt_tag" && ( -n "$opt_debs_dir" || -n "$opt_from_repo" ) ]]; then
		die "--tag only applies when build.sh builds the packages itself."
	fi
	# Resolve relative paths before changing directory.
	if [[ -n "$opt_debs_dir" ]]; then
		opt_debs_dir="$(readlink -f -- "$opt_debs_dir")"
		[[ -d "$opt_debs_dir" ]] || die "--debs-dir: $opt_debs_dir is not a directory."
	fi
	if [[ -n "$opt_output" ]]; then
		opt_output="$(readlink -m -- "$opt_output")"
	fi

	nimbus_load_conf
	opt_suite="${opt_suite:-$NIMBUS_APT_DEFAULT_SUITE}"
	case " $NIMBUS_APT_SUITES " in
		*" $opt_suite "*) ;;
		*) die "--suite must be one of: $NIMBUS_APT_SUITES" ;;
	esac

	# --- 1. Host -------------------------------------------------------------
	step "Build host"
	nimbus_check_host
	nimbus_apt_install_missing live-build debootstrap devscripts dpkg-dev debhelper \
		gpg gpgv curl ca-certificates git librsvg2-bin xz-utils
	export LC_ALL=C.UTF-8

	work="$(nimbus_path "$NIMBUS_WORK_DIR")"
	out="${opt_output:-$(nimbus_path "$NIMBUS_OUTPUT_DIR")}"
	mkdir -p -- "$work" "$out"
	exec 9>"$work/.build.lock"
	flock -n 9 || die "Another build is already running in $work."
	free="$(nimbus_free_gib "$work")"
	((free >= NIMBUS_MIN_FREE_GIB)) \
		|| die "Only ${free} GiB free on the file system of $work; at least ${NIMBUS_MIN_FREE_GIB} GiB are needed."
	log "Free space: ${free} GiB"

	# --- 2. Keys and repository ----------------------------------------------
	step "Keys and APT repository"
	nimbus_check_docker_key
	if [[ -n "$opt_skip_repo_check" ]]; then
		warn "Skipping the APT repository check (--skip-repo-check). If $NIMBUS_APT_URL is unreachable, lb build will fail at the end of the chroot stage."
	else
		nimbus_check_repo "$NIMBUS_APT_DEFAULT_SUITE"
		if [[ -n "$opt_from_repo" && "$opt_suite" != "$NIMBUS_APT_DEFAULT_SUITE" ]]; then
			nimbus_check_repo "$opt_suite"
		fi
	fi

	# --- 3. Packages ---------------------------------------------------------
	step "NimbusOS packages"
	debs="$work/debs"
	rm -rf -- "$debs"
	mkdir -p -- "$debs"
	if [[ -n "$opt_debs_dir" ]]; then
		for pkg in "${NIMBUS_PACKAGES[@]}"; do
			find "$opt_debs_dir" -maxdepth 1 -type f -name "${pkg}_*.deb" -exec cp -- {} "$debs/" \;
		done
		log "Using packages from $opt_debs_dir"
	elif [[ -n "$opt_from_repo" ]]; then
		fetch_from_repo "$debs"
	else
		if [[ -n "$opt_tag" ]]; then
			build_args=(--tag "$opt_tag")
		fi
		"$NIMBUS_REPO_ROOT/tools/build-debs.sh" --out "$debs" --install-build-deps "${build_args[@]}"
	fi
	version="$(nimbus_check_deb_set "$debs")"
	nimbus_check_keyring_deb "$(find "$debs" -maxdepth 1 -name 'nimbusos-archive-keyring_*.deb' -print -quit)"
	display="${version/\~beta./-beta.}"
	log "Packages OK: ${NIMBUS_NAME} $display ($version)"

	# Values available to @NIMBUS_*@ tokens in build/config/.
	NIMBUS_VERSION="$version"
	NIMBUS_DISPLAY_VERSION="$display"
	export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(nimbus_git log -1 --format=%ct 2>/dev/null || date -u +%s)}"

	# --- 4. live-build tree --------------------------------------------------
	step "Preparing $work"
	cd -- "$work"
	if [[ -d config ]]; then
		if [[ -n "$opt_clean_cache" ]]; then
			lb clean --purge
		else
			lb clean
		fi
	fi
	# A fresh copy of the template every time, and a fresh bootstrap (the
	# downloaded-package cache is kept unless --clean-cache).
	rm -rf -- "$work/config" "$work/cache/bootstrap"
	cp -R --preserve=mode,timestamps -- "$NIMBUS_REPO_ROOT/build/config" "$work/config"
	nimbus_render_tree "$work/config"
	if [[ -d "$work/config/hooks" ]]; then
		find "$work/config/hooks" -type f -name '*.hook.*' -exec chmod 0755 {} +
	fi
	mkdir -p -- "$work/config/packages.chroot"
	cp -- "$debs"/*.deb "$work/config/packages.chroot/"
	prepare_bootloaders

	# --- 5. live-build -------------------------------------------------------
	step "lb config"
	nimbus_lb_config_args "$display"
	lb config "${NIMBUS_LB_ARGS[@]}" 2>&1 | tee "$work/lb-config.log" >&2

	step "lb build (log: $work/build.log)"
	lb build 2>&1 | tee "$work/build.log" >&2

	# --- 6. Output -----------------------------------------------------------
	step "Output"
	iso_src="$work/${NIMBUS_IMAGE_NAME}-${NIMBUS_ARCH}.hybrid.iso"
	[[ -f "$iso_src" ]] || die "lb build finished without producing $(basename "$iso_src"); see $work/build.log."
	iso="$out/${NIMBUS_ISO_BASENAME}.iso"
	mv -f -- "$iso_src" "$iso"
	manifest="$work/${NIMBUS_IMAGE_NAME}-${NIMBUS_ARCH}.packages"
	if [[ -f "$manifest" ]]; then
		cp -- "$manifest" "$out/${NIMBUS_ISO_BASENAME}.packages"
	fi
	(cd -- "$out" && sha256sum -- "$(basename "$iso")" >"$(basename "$iso").sha256")

	size="$(stat -c %s -- "$iso")"
	limit="$NIMBUS_RELEASE_ASSET_LIMIT_BYTES"
	log "ISO:      $iso ($((size / 1024 / 1024)) MiB)"
	log "SHA-256:  $(cut -d' ' -f1 "$iso.sha256")"
	log "Version:  ${NIMBUS_NAME} $display ($version)"
	if ((size >= limit)); then
		log "The ISO is larger than GitHub's per-asset limit; release.yml uploads it as ${NIMBUS_ISO_SPLIT_MIB} MiB parts (docs/PLAN.md NOTE-41)."
	fi
}

main "$@"
