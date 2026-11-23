#!/bin/bash
# =============================================================================
# tools/tests/split-iso.test.sh
# =============================================================================
# What: runs tools/split-iso.sh on fake ISOs with a small limit (3 MiB, parts
#       of 1 MiB) and checks:
#         - a 5 MiB ISO becomes 5 parts; SHA256SUMS lists the ISO and every
#           part; `sha256sum -c` accepts the parts, and joining them with
#           cat gives the ISO back (and passes its SHA256SUMS line);
#         - the instructions give the signature check, the Linux join and
#           check, and the PowerShell join and check, with the fingerprint;
#         - a 2 MiB ISO is uploaded whole, with one SHA256SUMS line, and its
#           instructions have no join step;
#         - parts as large as the limit are refused.
# Why:  docs/PLAN.md NOTE-41 (spec 4.3: release assets under 2 GiB).
#
# Usage: tools/tests/split-iso.test.sh
#        Needs bash and coreutils.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
fails() { ! "$@"; }
nimbus_mktemp_dir T
SPLIT=("$ROOT/tools/split-iso.sh" --limit 3145728 --part-mib 1)
FPR=0123456789ABCDEF0123456789ABCDEF01234567

head -c 5242880 /dev/urandom >"$T/big.iso"
head -c 2097152 /dev/urandom >"$T/small.iso"
count_parts() { [[ "$(find "$T/out" -name 'big.iso.part*' | wc -l)" == "$1" ]]; }
in_sums() { grep -qE "^[0-9a-f]{64}  $1\$" "$2/SHA256SUMS"; }
parts_check() { (cd "$T/out" && grep '\.part[0-9]*$' SHA256SUMS | sha256sum -c); }
joined_check() { (cd "$T/out" && cat big.iso.part* >big.iso && grep ' big.iso$' SHA256SUMS | sha256sum -c && cmp -s big.iso "$T/big.iso"); }
has() { grep -qF -- "$1" "$2"; }

echo "an ISO over the limit"
check "split-iso.sh runs" "${SPLIT[@]}" --iso "$T/big.iso" --out "$T/out" --instructions "$T/big.md" --fingerprint "$FPR"
check "it is split into 5 parts of 1 MiB" count_parts 5
check "the whole ISO is not uploaded" test ! -e "$T/out/big.iso"
check "SHA256SUMS lists the ISO" in_sums big.iso "$T/out"
check "SHA256SUMS lists every part" in_sums big.iso.part04 "$T/out"
check "sha256sum -c accepts the parts" parts_check
check "cat joins them back into the ISO, which matches SHA256SUMS" joined_check
check "the instructions check the signature with the key from the repository" has "gpg --verify SHA256SUMS.gpg SHA256SUMS" "$T/big.md"
check "  … and quote the fingerprint" has "$FPR" "$T/big.md"
check "  … join on Linux" has "cat big.iso.part* > big.iso" "$T/big.md"
check "  … and in PowerShell" has '$in.CopyTo($out)' "$T/big.md"
check "  … and check the result in PowerShell" has "Get-FileHash big.iso -Algorithm SHA256" "$T/big.md"

echo "an ISO under the limit"
check "split-iso.sh runs" "${SPLIT[@]}" --iso "$T/small.iso" --out "$T/out-small" --instructions "$T/small.md"
check "it is uploaded whole" cmp -s "$T/small.iso" "$T/out-small/small.iso"
check "SHA256SUMS has its one line" test "$(wc -l <"$T/out-small/SHA256SUMS")" = 1
check "the instructions have no join step" fails has ".part" "$T/small.md"

echo "bad settings"
check "parts as large as the limit are refused" fails "$ROOT/tools/split-iso.sh" --iso "$T/big.iso" --out "$T/x" --limit 1048576 --part-mib 1

echo
echo "split-iso.test: $passed passed, $failed failed"
((failed == 0))
