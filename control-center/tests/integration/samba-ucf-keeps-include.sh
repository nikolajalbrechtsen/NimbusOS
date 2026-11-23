#!/bin/bash
# =============================================================================
# control-center/tests/integration/samba-ucf-keeps-include.sh
# =============================================================================
# What: proves that a Samba upgrade keeps NimbusOS's include line in
#       /etc/samba/smb.conf, in a throwaway debian:trixie container:
#         1. installs samba; Debian's samba-common manages smb.conf with ucf;
#         2. appends the marked include block exactly as nimbus-control's
#            postinst writes it (its lines are read from that postinst, so
#            the test can't drift from it);
#         3. changes the packaged template /usr/share/samba/smb.conf as a
#            new Samba release would, and runs samba-common's own postinst
#            through dpkg-reconfigure, which runs what an upgrade runs:
#              ucf --three-way --debconf-ok /usr/share/samba/smb.conf /etc/samba/smb.conf
#            with UCF_FORCE_CONFFOLD=1, as nimbus-update sets it: the include
#            is still there, and ucf still owns the file;
#         4. the same without UCF_FORCE_CONFFOLD in a non-interactive run
#            (unattended upgrades, Discover): the include is kept too;
#         5. with UCF_FORCE_CONFFNEW=1 (an administrator choosing Samba's
#            new file) the include is gone, and nimbus-control's postinst
#            block puts it back exactly once (privd does the same at
#            startup and before applying shares, NOTE-23).
# Why:  docs/PLAN.md NOTE-23 (requested in review): ucf, not dpkg, manages
#       smb.conf, so --force-confold alone doesn't decide what happens.
#
# Usage: control-center/tests/integration/samba-ucf-keeps-include.sh
#        Root, in a throwaway container only (tools/dev-shell.sh). Network
#        (deb.debian.org).
# =============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
POSTINST="$REPO/packages/nimbus-control/debian/postinst"
[[ "$(id -u)" -eq 0 ]] || { echo "samba-ucf: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] || { echo "samba-ucf: throwaway container only" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
fails() { ! "$@"; }

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends samba >/tmp/samba-ucf-install.log 2>&1 \
	|| { tail -n 20 /tmp/samba-ucf-install.log; exit 1; }
echo "samba $(dpkg-query -W -f='${Version}' samba), ucf $(dpkg-query -W -f='${Version}' ucf)"

CONF=/etc/samba/smb.conf
TEMPLATE=/usr/share/samba/smb.conf
include="$(sed -n 's/^SMB_INCLUDE="\(.*\)"$/\1/p' "$POSTINST")"
begin="$(grep -o '"# BEGIN nimbus-control[^"]*"' "$POSTINST" | tr -d '"')"
end="$(grep -o '"# END nimbus-control"' "$POSTINST" | tr -d '"')"
[[ -n "$include" && -n "$begin" && -n "$end" ]] || { echo "samba-ucf: can't read the include block from $POSTINST" >&2; exit 2; }

add_block() {   # nimbus-control's postinst, step 1
	if ! grep -qxF "$include" "$CONF"; then
		printf '\n%s\n%s\n%s\n' "$begin" "$include" "$end" >>"$CONF"
	fi
}
has_include() { grep -qxF "$include" "$CONF"; }
include_count() { grep -cxF "$include" "$CONF" || true; }
owned_by_samba_common() { ucfq -w "$CONF" | grep -q '^/etc/samba/smb.conf:samba-common:'; }
new_samba_release() {   # the template changes, as in a new upstream release; then the upgrade's postinst
	printf '\n# New in this Samba release (test %s)\n' "$1" >>"$TEMPLATE"
	env "${@:2}" dpkg-reconfigure --frontend=noninteractive samba-common >>/tmp/samba-ucf.log 2>&1
}

echo "Setup"
check "ucf manages smb.conf for samba-common" owned_by_samba_common
add_block
check "the include block is appended" has_include

echo "A Samba upgrade, as nimbus-update runs it (UCF_FORCE_CONFFOLD=1)"
check "samba-common's postinst runs" new_samba_release 1 UCF_FORCE_CONFFOLD=1
check "the include line is still there" has_include
check "smb.conf is still registered to samba-common" owned_by_samba_common
check "the new template was not merged in" fails grep -q "New in this Samba release (test 1)" "$CONF"

echo "A non-interactive upgrade without UCF_FORCE_CONFFOLD"
check "samba-common's postinst runs" new_samba_release 2 DEBIAN_FRONTEND=noninteractive
check "the include line is still there" has_include

echo "An administrator choosing Samba's new file (UCF_FORCE_CONFFNEW=1)"
check "samba-common's postinst runs" new_samba_release 3 UCF_FORCE_CONFFNEW=1
check "the include line is gone (as expected)" fails has_include
add_block
check "nimbus-control's postinst block puts it back" has_include
add_block
check "  … exactly once, however often it runs" test "$(include_count)" = 1

echo
echo "samba-ucf-keeps-include: $passed passed, $failed failed"
((failed == 0)) || { tail -n 20 /tmp/samba-ucf.log; exit 1; }
