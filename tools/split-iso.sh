#!/bin/bash
# =============================================================================
# tools/split-iso.sh: the ISO's release assets, split if GitHub needs it
# =============================================================================
# What: puts into OUT_DIR what release.yml uploads for an ISO:
#         - the ISO itself, if it is smaller than NIMBUS_RELEASE_ASSET_LIMIT_BYTES
#           (GitHub's 2 GiB per release asset); otherwise parts of
#           NIMBUS_ISO_SPLIT_MIB MiB named <iso>.part00, .part01, …;
#         - SHA256SUMS, listing the whole ISO and every part, so a download
#           can be checked before and after joining (signed later by
#           release.yml as SHA256SUMS.gpg);
#       and, with --instructions, the "Download and verify" text for the
#       release page: checking the signature, then checking (and joining)
#       the files on Linux, macOS and Windows PowerShell.
# Why:  spec 4.3 and docs/PLAN.md NOTE-41: a Plasma desktop with Docker,
#       QEMU and firmware is larger than GitHub's limit for one file.
#
# Usage: tools/split-iso.sh --iso FILE --out DIR [--instructions FILE]
#                           [--fingerprint FPR] [--limit BYTES] [--part-mib N]
#   --fingerprint FPR  the signing key's fingerprint, quoted in the instructions
#   --limit, --part-mib  override nimbus.conf (used by tests)
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

iso="" out="" instructions="" fingerprint="" limit="" part_mib=""
while (($#)); do
	case "$1" in
		--iso) iso="${2:?}"; shift 2 ;;
		--out) out="${2:?}"; shift 2 ;;
		--instructions) instructions="${2:?}"; shift 2 ;;
		--fingerprint) fingerprint="${2:?}"; shift 2 ;;
		--limit) limit="${2:?}"; shift 2 ;;
		--part-mib) part_mib="${2:?}"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) die "Unknown option: $1 (see --help)" ;;
	esac
done
nimbus_load_conf
limit="${limit:-$NIMBUS_RELEASE_ASSET_LIMIT_BYTES}"
part_mib="${part_mib:-$NIMBUS_ISO_SPLIT_MIB}"
[[ -n "$iso" && -n "$out" ]] || die "--iso and --out are required (see --help)."
[[ -f "$iso" ]] || die "No ISO at $iso."
[[ "$limit" =~ ^[0-9]+$ && "$part_mib" =~ ^[1-9][0-9]*$ ]] || die "--limit and --part-mib must be whole numbers."
((part_mib * 1048576 < limit)) || die "Parts of $part_mib MiB would not be under the $limit-byte limit."

name="$(basename "$iso")"
size="$(stat -c %s "$iso")"
mkdir -p -- "$out"
rm -f -- "$out/$name" "$out/$name".part* "$out/SHA256SUMS"

iso_sum="$(sha256sum <"$iso" | cut -d' ' -f1)"
if ((size < limit)); then
	split=0
	ln -f -- "$iso" "$out/$name" 2>/dev/null || cp -- "$iso" "$out/$name"
	printf '%s  %s\n' "$iso_sum" "$name" >"$out/SHA256SUMS"
	log "$name is $size bytes, under the $limit-byte limit: uploaded whole."
else
	split=1
	split --bytes="${part_mib}M" --numeric-suffixes=0 --suffix-length=2 -- "$iso" "$out/$name.part"
	{
		printf '%s  %s\n' "$iso_sum" "$name"
		(cd "$out" && sha256sum -- "$name".part*)
	} >"$out/SHA256SUMS"
	# Joining the parts must give the ISO back, byte for byte.
	[[ "$(cat -- "$out/$name".part* | sha256sum | cut -d' ' -f1)" == "$iso_sum" ]] \
		|| die "The joined parts don't match $name."
	log "$name is $size bytes: split into $(find "$out" -name "$name.part*" | wc -l) parts of at most $part_mib MiB."
fi

[[ -n "$instructions" ]] || exit 0
keyring_url="$NIMBUS_APT_URL/nimbusos-archive-keyring.asc"
{
	echo "## Download and verify"
	echo
	if ((split)); then
		echo "The ISO is $(numfmt --to=iec-i --suffix=B "$size"), more than GitHub allows for one file, so it comes in"
		echo "parts of at most $part_mib MiB: download every \`$name.part*\` file, \`SHA256SUMS\` and \`SHA256SUMS.gpg\`."
	else
		echo "Download \`$name\`, \`SHA256SUMS\` and \`SHA256SUMS.gpg\`."
	fi
	echo
	echo "**1. Check the signature** (GnuPG; on Windows from Gpg4win):"
	echo
	echo '```sh'
	echo "curl -fsSLO $keyring_url"
	echo "gpg --show-keys nimbusos-archive-keyring.asc"
	echo "gpg --import nimbusos-archive-keyring.asc"
	echo "gpg --verify SHA256SUMS.gpg SHA256SUMS"
	echo '```'
	echo
	if [[ -n "$fingerprint" ]]; then
		echo "\`gpg --show-keys\` must show the fingerprint \`$fingerprint\`, and \`gpg --verify\` must say \"Good signature\"."
	else
		echo "\`gpg --verify\` must say \"Good signature\" from the NimbusOS archive key."
	fi
	echo
	echo "**2. Check the files** on Linux (on macOS, use \`shasum -a 256 -c\` instead of \`sha256sum -c\`):"
	echo
	echo '```sh'
	if ((split)); then
		echo "grep '\\.part[0-9]*\$' SHA256SUMS | sha256sum -c"
		echo "cat $name.part* > $name"
	fi
	echo "grep ' $name\$' SHA256SUMS | sha256sum -c"
	echo '```'
	echo
	echo "On Windows, in PowerShell, in the download folder:"
	echo
	echo '```powershell'
	if ((split)); then
		echo "\$parts = Get-ChildItem $name.part* | Sort-Object Name"
		echo "\$out = [System.IO.File]::Create((Join-Path \$PWD '$name'))"
		echo "foreach (\$p in \$parts) { \$in = [System.IO.File]::OpenRead(\$p.FullName); \$in.CopyTo(\$out); \$in.Dispose() }"
		echo "\$out.Dispose()"
	fi
	echo "\$expected = ((Select-String -Path SHA256SUMS -Pattern ' $name\$').Line -split ' ')[0]"
	echo "\$actual = (Get-FileHash $name -Algorithm SHA256).Hash.ToLower()"
	echo "if (\$actual -eq \$expected) { 'OK' } else { 'MISMATCH: download again' }"
	echo '```'
} >"$instructions"
log "Wrote the download instructions to $instructions."
