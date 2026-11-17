#!/bin/bash
# =============================================================================
# packages/nimbusos-release/tests/recommends.test.sh
# =============================================================================
# What: proves, with real apt and the real nimbus-update, that the
#       Depends/Recommends split of nimbusos-desktop behaves as intended
#       (docs/PLAN.md NOTE-48). A local repository serves stand-in packages
#       shaped like the real ones:
#         v1  nimbusos-desktop 1.0  Depends core-runtime; Recommends firefox-esr, konsole
#         v2  nimbusos-desktop 2.0  ... adds Recommends new-app
#         v3  nimbusos-desktop 3.0  ... adds Recommends another-app
#       Steps: install v1 (both apps arrive); remove firefox-esr, and
#       nimbusos-desktop must stay installed; publish v2 and run
#       `nimbus-update auto`, so new-app must be installed and firefox-esr must
#       stay removed; publish v3 and run `nimbus-update install`, with the
#       same checks; remove nimbusos-desktop, and `nimbus-update status` must
#       show the warning the Updates page displays.
#
# Why:  removing one application must never silently stop a machine from
#       receiving new NimbusOS software, and newly recommended software must
#       arrive through both update paths.
#
# Usage: packages/nimbusos-release/tests/recommends.test.sh DEBS_DIR
#        DEBS_DIR holds the built nimbusos-archive-keyring and nimbusos-release
#        .debs. Throwaway container only (tools/dev-shell.sh): it installs
#        packages and replaces the APT sources with the local repository.
# =============================================================================
set -Eeuo pipefail

DEBS="$(readlink -f "${1:?usage: recommends.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "recommends.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "recommends.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
T="$(mktemp -d)"
trap 'kill "${HTTP_PID:-}" 2>/dev/null || true; rm -rf "$T"' EXIT

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
installed() { [[ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null)" == installed ]]; }
version_of() { dpkg-query -W -f='${Version}' "$1" 2>/dev/null; }

# --- Real NimbusOS release tooling (from Debian + the built .debs) -----------
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends python3 apt-utils dpkg-dev \
	"$(find "$DEBS" -maxdepth 1 -name 'nimbusos-archive-keyring_*.deb' -print -quit)" \
	"$(find "$DEBS" -maxdepth 1 -name 'nimbusos-release_*.deb' -print -quit)" >"$T/setup.log" 2>&1 \
	|| { cat "$T/setup.log"; exit 1; }
# From here on the only package source is the local test repository.
mkdir -p "$T/apt-sources-off"
mv /etc/apt/sources.list.d/* "$T/apt-sources-off/" 2>/dev/null || true
: >/etc/apt/sources.list
echo "deb [trusted=yes] http://127.0.0.1/repo/ ./" >/etc/apt/sources.list.d/test.list

# --- Stand-in packages and repository ------------------------------------------
make_deb() {   # make_deb NAME VERSION [DEPENDS] [RECOMMENDS]
	local root="$T/build/$1-$2"
	mkdir -p "$root/DEBIAN" "$T/www/repo"
	{
		echo "Package: $1"
		echo "Version: $2"
		echo "Architecture: all"
		echo "Maintainer: NimbusOS Test <test@example.invalid>"
		[[ -n "${3:-}" ]] && echo "Depends: $3"
		[[ -n "${4:-}" ]] && echo "Recommends: $4"
		echo "Description: stand-in package $1"
	} >"$root/DEBIAN/control"
	dpkg-deb --root-owner-group --build "$root" "$T/www/repo/${1}_${2}_all.deb" >/dev/null
}
# The test web server answers "304 Not Modified" if a regenerated Release has
# the same one-second mtime as the previous one, so each publish waits a
# second (GitHub Pages deploys are minutes apart; this only affects the test).
publish() {
	sleep 1
	(cd "$T/www/repo" && apt-ftparchive packages . >Packages && apt-ftparchive release . >Release)
	apt-get update -q >/dev/null
}

# Realistic versions matter: Debian's libc6 Breaks firefox-esr (<< 91~), so a
# stand-in firefox-esr 1.0 would (correctly) never be installed.
make_deb core-runtime 1.0
make_deb firefox-esr 140.5.0esr-1
make_deb konsole 25.04.3-1
make_deb nimbusos-desktop 1.0 "core-runtime" "firefox-esr, konsole"
python3 -m http.server 80 --bind 127.0.0.1 --directory "$T/www" >"$T/http.log" 2>&1 &
HTTP_PID=$!
for _ in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/80) 2>/dev/null && break; sleep 0.1; done
publish

echo "v1: install, then remove one recommended application"
apt-get install -y -q nimbusos-desktop >"$T/v1.log" 2>&1 || { cat "$T/v1.log"; exit 1; }
check "nimbusos-desktop 1.0 installed" test "$(version_of nimbusos-desktop)" = 1.0
check "recommended firefox-esr and konsole installed with it" sh -c 'dpkg -s firefox-esr konsole >/dev/null'
apt-get remove -y -q firefox-esr >"$T/remove.log" 2>&1
check "removing firefox-esr keeps nimbusos-desktop installed" installed nimbusos-desktop
check "firefox-esr is removed" sh -c "! dpkg-query -W -f='\${db:Status-Status}' firefox-esr 2>/dev/null | grep -qx installed"

echo "v2: nimbus-update auto (Automatic policy) brings a new recommended package"
make_deb new-app 1.0
make_deb nimbusos-desktop 2.0 "core-runtime" "firefox-esr, konsole, new-app"
publish
nimbus-update auto >"$T/auto.log" 2>&1 || { cat "$T/auto.log"; exit 1; }
check "nimbusos-desktop upgraded to 2.0" test "$(version_of nimbusos-desktop)" = 2.0
check "newly recommended new-app was installed" installed new-app
check "the removed firefox-esr was not reinstalled" sh -c "! dpkg-query -W -f='\${db:Status-Status}' firefox-esr 2>/dev/null | grep -qx installed"

echo "v3: nimbus-update install (\"Install updates\") does the same"
make_deb another-app 1.0
make_deb nimbusos-desktop 3.0 "core-runtime" "firefox-esr, konsole, new-app, another-app"
publish
nimbus-update install >"$T/install.log" 2>&1 || { cat "$T/install.log"; exit 1; }
check "nimbusos-desktop upgraded to 3.0" test "$(version_of nimbusos-desktop)" = 3.0
check "newly recommended another-app was installed" installed another-app
check "the removed firefox-esr was still not reinstalled" sh -c "! dpkg-query -W -f='\${db:Status-Status}' firefox-esr 2>/dev/null | grep -qx installed"

echo "Warning when nimbusos-desktop is gone"
check "status reports the metapackage as installed" \
	sh -c 'nimbus-update --json status | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin)[\"desktop_metapackage_installed\"] else 1)"'
apt-get remove -y -q nimbusos-desktop >/dev/null 2>&1
check "status warns: This system no longer receives new NimbusOS software" \
	sh -c 'nimbus-update status | grep -q "This system no longer receives new NimbusOS software. Reinstall nimbusos-desktop"'

if ((failed)); then
	for log in v1 auto install; do
		echo "----- $log.log (tail) -----"
		tail -n 25 "$T/$log.log" 2>/dev/null || true
	done
	echo "----- apt config affecting Recommends -----"
	apt-config dump | grep -i -E 'recommend' || true
fi
echo
printf 'recommends.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
