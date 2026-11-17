#!/bin/bash
# =============================================================================
# packages/nimbusos-release/tests/os-identity.test.sh
# =============================================================================
# What: with NimbusOS's os-release installed, checks everything that derives
#       the Debian release from the system's release information, and prints
#       the evidence (docs/PLAN.md §7):
#         - os-release keeps ID_LIKE=debian, VERSION_CODENAME and
#           DEBIAN_CODENAME = trixie; /etc/debian_version is base-files' own;
#         - lsb_release -a (lsb-release, which unattended-upgrades uses for
#           ${distro_codename}) still reports the codename trixie;
#         - unattended-upgrade --dry-run --debug: the allowed origins expand to
#           Debian / trixie-security only, and a package with a pending
#           security update (ntfs-3g, installed at its older trixie version)
#           is picked up;
#         - apt-get -s dist-upgrade sees that update from Debian-Security;
#         - Docker's source (the real docker.list.binary, rendered as build.sh
#           renders it) resolves to trixie, as does Docker's documented
#           $(. /etc/os-release; echo "$VERSION_CODENAME"), and
#           apt-cache policy shows docker-ce from download.docker.com;
#         - Calamares (3.3.14 and calamares-settings-debian): where its
#           modules and Debian's helper scripts read the release.
#
# Why:  NimbusOS replaces /usr/lib/os-release (NOTE-36). Anything that keys
#       on the Debian codename must keep working, above all automatic
#       security updates.
#
# Usage: packages/nimbusos-release/tests/os-identity.test.sh DEBS_DIR
#        Network test (deb.debian.org, security.debian.org,
#        download.docker.com). Throwaway container only (tools/dev-shell.sh).
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd -P)"
DEBS="$(readlink -f "${1:?usage: os-identity.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "os-identity.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "os-identity.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
show() { printf '\n  $ %s\n' "$1"; sed 's/^/    /'; }

# Debian sources with security updates (the container image may only have main).
cat >/etc/apt/sources.list.d/debian.sources <<'EOF'
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp
EOF
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends \
	"$(find "$DEBS" -maxdepth 1 -name 'nimbusos-archive-keyring_*.deb' -print -quit)" \
	"$(find "$DEBS" -maxdepth 1 -name 'nimbusos-docker-keyring_*.deb' -print -quit)" \
	"$(find "$DEBS" -maxdepth 1 -name 'nimbusos-release_*.deb' -print -quit)" >/tmp/setup.log 2>&1 \
	|| { cat /tmp/setup.log; exit 1; }

# The image's Docker source, exactly as build.sh renders it.
# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"
nimbus_load_conf
cp "$ROOT/build/config/archives/docker.list.binary" /etc/apt/sources.list.d/docker.list
nimbus_render_tokens /etc/apt/sources.list.d/docker.list

echo "Release identity"
grep -E '^(NAME|ID|ID_LIKE|VERSION_CODENAME|DEBIAN_CODENAME|NIMBUSOS_CHANNEL)=' /etc/os-release | show "grep ID/CODENAME /etc/os-release"
{ echo "/etc/debian_version: $(cat /etc/debian_version) ($(dpkg -S /etc/debian_version))"; } | show "cat /etc/debian_version"
lsb_release -a 2>/dev/null | show "lsb_release -a"
check "ID_LIKE=debian" grep -qx 'ID_LIKE=debian' /etc/os-release
check "VERSION_CODENAME=trixie" grep -qx 'VERSION_CODENAME=trixie' /etc/os-release
check "DEBIAN_CODENAME=trixie" grep -qx 'DEBIAN_CODENAME=trixie' /etc/os-release
check "/etc/debian_version belongs to base-files and still says 13.x" \
	sh -c 'dpkg -S /etc/debian_version | grep -q "^base-files:" && grep -Eq "^13(\.|$)" /etc/debian_version'
check "lsb_release -cs says trixie" test "$(lsb_release -cs 2>/dev/null)" = trixie

echo
echo "unattended-upgrades"
# A package with a pending security update: install its older trixie version.
security_version="$(apt-cache policy ntfs-3g | awk '/security.debian.org/ { print prev; exit } { prev = $1 }')"
apt-get install -y -q --no-install-recommends ntfs-3g/trixie >/tmp/ntfs.log 2>&1 || { cat /tmp/ntfs.log; exit 1; }
installed_version="$(dpkg-query -W -f='${Version}' ntfs-3g)"
echo "ntfs-3g installed: $installed_version; candidate from trixie-security: ${security_version:-none}" | show "dpkg-query ntfs-3g"
unattended-upgrade --dry-run --debug >/tmp/uu.log 2>&1 || true
grep -E 'Allowed origins are|Packages that will be upgraded|Initial (whitelist|blacklist)' /tmp/uu.log | show "unattended-upgrade --dry-run --debug (excerpt)"
check "allowed origins: only the two Debian-Security patterns, expanded to trixie" \
	grep -q "Allowed origins are: origin=Debian,codename=trixie,label=Debian-Security, origin=Debian,codename=trixie-security,label=Debian-Security" /tmp/uu.log
check "the pending security update (ntfs-3g) would be installed" grep -qE 'Packages that will be upgraded:.*ntfs-3g' /tmp/uu.log

apt-get -s dist-upgrade 2>/dev/null | grep -E '^Inst ' | show "apt-get -s dist-upgrade (Inst lines)"
check "apt-get -s dist-upgrade takes ntfs-3g from Debian-Security" \
	sh -c 'apt-get -s dist-upgrade | grep -E "^Inst ntfs-3g .*Debian-Security"'

echo
echo "Docker source"
# Refresh with the Docker source in place. The NimbusOS repository itself is
# not published yet, so its 404 is expected here: it must be reported as a
# problem with the NimbusOS repository, never as "no updates".
apt-get update -q >/tmp/update2.log 2>&1 || true
nimbus-update --json check >/tmp/check.json 2>/dev/null || true
python3 - /tmp/check.json <<'PYEOF' | show "nimbus-update check: problems reported"
import json, sys
status = json.load(open(sys.argv[1], encoding="utf-8"))
print("\n".join(f"{e['source']}: {e['kind']} ({e['host']})" for e in status["errors"]) or "no errors")
PYEOF
{ echo "docker.list: $(grep '^deb ' /etc/apt/sources.list.d/docker.list)"
  echo "Docker's documented codename lookup: $(. /etc/os-release && echo "$VERSION_CODENAME")"; } | show "Docker source and codename"
apt-cache policy docker-ce 2>/dev/null | sed -n '1,7p' | show "apt-cache policy docker-ce"
check "docker.list names trixie" grep -q ' trixie stable$' /etc/apt/sources.list.d/docker.list
check "docker-ce has a candidate from download.docker.com trixie/stable" \
	sh -c 'apt-cache policy docker-ce | grep -q "download.docker.com/linux/debian trixie/stable"'

echo
echo "Calamares"
cd /tmp && apt-get download -q calamares calamares-settings-debian >/dev/null 2>&1
mkdir -p cal && for d in calamares_*.deb calamares-settings-debian_*.deb; do dpkg-deb -x "$d" cal; done
{ grep -rIn -E 'VERSION_CODENAME|lsb_release|distro_codename|RELEASE=' cal/usr/share/calamares/helpers cal/etc/calamares 2>/dev/null || true
  grep -rl -E 'os-release|VERSION_CODENAME' cal/usr/lib 2>/dev/null | sed 's|^cal||;s|$|  (mentions os-release or VERSION_CODENAME)|' || true
} | show "where Calamares reads the release"
check "Debian's Calamares helpers hard-code RELEASE=\"trixie\" (independent of os-release)" \
	grep -q 'RELEASE="trixie"' cal/usr/share/calamares/helpers/calamares-sources-media

echo
printf 'os-identity.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
