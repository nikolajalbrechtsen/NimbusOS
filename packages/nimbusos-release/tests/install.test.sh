#!/bin/bash
# =============================================================================
# packages/nimbusos-release/tests/install.test.sh
# =============================================================================
# What: installs the built nimbusos-archive-keyring and nimbusos-release
#       packages into the current (throwaway) Debian system and checks what
#       their maintainer scripts and files do:
#         - base-files' os-release is diverted, /etc/os-release says NimbusOS;
#         - /etc/nimbusos/update.conf is created, and kept on reinstall;
#         - unattended-upgrades is limited to the two Debian-Security
#           patterns (the "#clear" in 52nimbusos-unattended-upgrades works);
#         - the timers and the Flathub unit are enabled;
#         - nimbus-update status/channel/policy work, Beta adds and Stable
#           removes nimbusos-beta.sources;
#         - removal restores Debian's os-release; purge removes the state.
#
# Why:  maintainer scripts run as root on every installed system, during
#       image builds and on every upgrade; they must be correct and
#       idempotent (spec 4.1).
#
# Usage: packages/nimbusos-release/tests/install.test.sh DEBS_DIR
#        DEBS_DIR holds the .debs from tools/build-debs.sh. Run it only in a
#        throwaway container (tools/dev-shell.sh): it installs and removes
#        packages system-wide.
# =============================================================================
set -Eeuo pipefail

DEBS="$(readlink -f "${1:?usage: install.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "install.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "install.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
export DEBIAN_FRONTEND=noninteractive

keyring="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-archive-keyring_*.deb' -print -quit)"
release="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-release_*.deb' -print -quit)"
[[ -n "$keyring" && -n "$release" ]] || { echo "install.test: no keyring/release .deb in $DEBS" >&2; exit 2; }
cp /usr/lib/os-release /tmp/os-release.before-nimbusos

echo "Install"
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends "$keyring" "$release" >/tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
check "os-release is diverted to /usr/lib/os-release.debian" \
	sh -c 'dpkg-divert --list /usr/lib/os-release | grep -q "os-release.debian by nimbusos-release"'
check "/etc/os-release identifies NimbusOS" grep -qx 'ID=nimbusos' /etc/os-release
check "VERSION_CODENAME stays trixie (unattended-upgrades, Docker)" grep -qx 'VERSION_CODENAME=trixie' /etc/os-release
check "Debian's file is kept as os-release.debian" grep -qx 'ID=debian' /usr/lib/os-release.debian
check "update.conf is created with the defaults" \
	sh -c 'grep -qx CHANNEL=stable /etc/nimbusos/update.conf && grep -qx POLICY=automatic /etc/nimbusos/update.conf'
check "the NimbusOS source names the archive keyring" \
	grep -q '^Signed-By: /usr/share/keyrings/nimbusos-archive-keyring.gpg$' /etc/apt/sources.list.d/nimbusos.sources
check "no unrendered @NIMBUS_ token is installed" \
	sh -c '! grep -rIl "@NIMBUS_" /usr/lib/os-release /etc/apt /usr/bin/nimbus-update /usr/lib/systemd/system/nimbus-* /usr/lib/nimbusos'
patterns="$(apt-config dump Unattended-Upgrade::Origins-Pattern | grep -c '^Unattended-Upgrade::Origins-Pattern:: ' || true)"
check "unattended-upgrades allows exactly the 2 Debian-Security patterns (#clear works)" test "$patterns" = 2
check "unattended-upgrades never reboots" \
	sh -c 'apt-config dump Unattended-Upgrade::Automatic-Reboot | grep -q "\"false\""'
check "nimbus-update-check.timer is enabled" test -L /etc/systemd/system/timers.target.wants/nimbus-update-check.timer
check "nimbus-update-auto.timer is enabled" test -L /etc/systemd/system/timers.target.wants/nimbus-update-auto.timer
check "nimbus-flathub-setup.service is enabled" test -L /etc/systemd/system/multi-user.target.wants/nimbus-flathub-setup.service

echo "nimbus-update"
check "status --json is valid JSON" sh -c 'nimbus-update --json status | python3 -m json.tool'
check "channel beta adds the beta source" \
	sh -c 'nimbus-update channel beta --no-check && grep -q "^Suites: beta$" /etc/apt/sources.list.d/nimbusos-beta.sources'
check "policy notify is stored" sh -c 'nimbus-update policy notify && grep -qx POLICY=notify /etc/nimbusos/update.conf'
check "reinstall keeps the user's choices and the beta source" \
	sh -c "apt-get install -y -q --reinstall '$release' && grep -qx CHANNEL=beta /etc/nimbusos/update.conf \
	&& grep -qx POLICY=notify /etc/nimbusos/update.conf && test -f /etc/apt/sources.list.d/nimbusos-beta.sources"
check "channel stable removes the beta source" \
	sh -c 'nimbus-update channel stable --no-check && ! test -e /etc/apt/sources.list.d/nimbusos-beta.sources'
check "non-root use of install is refused with status 2" \
	sh -c 'command -v setpriv >/dev/null && { setpriv --reuid=65534 --regid=65534 --clear-groups nimbus-update install; test $? -eq 2; }'

echo "Remove and purge"
apt-get remove -y -q nimbusos-release >/dev/null 2>&1
check "removal gives /usr/lib/os-release back to base-files" cmp -s /usr/lib/os-release /tmp/os-release.before-nimbusos
check "removal deletes the diversion" sh -c '! dpkg-divert --list /usr/lib/os-release | grep -q nimbusos-release'
check "removal keeps update.conf (a purge removes it)" test -f /etc/nimbusos/update.conf
apt-get purge -y -q nimbusos-release >/dev/null 2>&1
check "purge removes /etc/nimbusos and /var/lib/nimbusos" sh -c '! test -e /etc/nimbusos && ! test -e /var/lib/nimbusos'

echo
printf 'install.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
