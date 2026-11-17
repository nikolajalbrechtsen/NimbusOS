#!/bin/bash
# =============================================================================
# packages/nimbusos-release/tests/apt-integration.test.sh
# =============================================================================
# What: runs real apt against three throwaway repositories served over HTTP
#       on loopback addresses, each with fake packages:
#         127.0.0.2  "Debian"    bash 5.2.37-2
#         127.0.0.1  "NimbusOS"  bash 99.0 (as if compromised), nimbus-control 0.2.0
#         127.0.0.3  "Docker"    bash 98.0 (as if compromised), docker-ce 29.0
#       With the shipped nimbusos.pref and nimbusos-docker.pref (rendered for
#       these hosts), `apt-cache policy` must pick bash from Debian,
#       nimbus-control from NimbusOS and docker-ce from Docker. The output is
#       printed: it is the apt-cache policy proof the spec asks for (4.2).
#       Then it feeds genuine apt 3 error output to nimbus-update's
#       classifier: an unsigned repository must be a "signature" error and an
#       unresolvable host a "dns" error.
#
# Why:  the pinning is the only thing that stops a broken or compromised
#       NimbusOS (or Docker) repository from replacing Debian packages, and
#       nimbus-update must never show a signature failure as "no updates".
#
# Usage: packages/nimbusos-release/tests/apt-integration.test.sh
#        Needs Debian as root (tools/dev-shell.sh): it binds port 80 on
#        loopback and installs python3, apt-utils and dpkg-dev if missing.
#        Uses no network outside the machine.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd -P)"
PKG="$ROOT/packages/nimbusos-release"
[[ "$(id -u)" -eq 0 ]] || { echo "apt-integration.test: run as root (tools/dev-shell.sh)" >&2; exit 2; }
if ! command -v python3 >/dev/null || ! command -v apt-ftparchive >/dev/null || ! command -v dpkg-deb >/dev/null; then
	DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends python3 apt-utils dpkg-dev >/dev/null
fi

# The pinning files are rendered for the loopback "hosts" used here.
export NIMBUS_APT_HOST=127.0.0.1 NIMBUS_DOCKER_APT_HOST=127.0.0.3
# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"
nimbus_load_conf
nimbus_mktemp_dir T

passed=0
failed=0
pass() { passed=$((passed + 1)); printf '  PASS  %s\n' "$1"; }
fail() { failed=$((failed + 1)); printf '  FAIL  %s\n' "$1"; }

# --- Fake packages and repositories -------------------------------------------
make_deb() {   # make_deb DIR NAME VERSION
	local dir="$1" name="$2" version="$3" root="$T/build/$2-$3"
	mkdir -p "$root/DEBIAN" "$dir"
	cat >"$root/DEBIAN/control" <<EOF
Package: $name
Version: $version
Architecture: all
Maintainer: NimbusOS Test <test@example.invalid>
Description: test package $name
EOF
	dpkg-deb --root-owner-group --build "$root" "$dir/${name}_${version}_all.deb" >/dev/null
}
make_repo() {   # make_repo DIR ORIGIN
	(cd "$1" && apt-ftparchive packages . >Packages \
		&& apt-ftparchive -o "APT::FTPArchive::Release::Origin=$2" -o "APT::FTPArchive::Release::Label=$2" release . >Release)
}
make_deb "$T/www/debian" bash 5.2.37-2
make_deb "$T/www/nimbusos" bash 99.0
make_deb "$T/www/nimbusos" nimbus-control 0.2.0
make_deb "$T/www/docker" bash 98.0
make_deb "$T/www/docker" docker-ce 29.0
make_repo "$T/www/debian" Debian
make_repo "$T/www/nimbusos" NimbusOS
make_repo "$T/www/docker" Docker

python3 -m http.server 80 --bind 0.0.0.0 --directory "$T/www" >"$T/http.log" 2>&1 &
HTTP_PID=$!
trap 'kill "$HTTP_PID" 2>/dev/null || true; _nimbus_cleanup' EXIT
for _ in $(seq 50); do
	if (exec 3<>/dev/tcp/127.0.0.1/80) 2>/dev/null; then break; fi
	sleep 0.1
done

# --- Pinning proof --------------------------------------------------------------
echo "Pinning (nimbusos.pref, nimbusos-docker.pref)"
APT="$T/apt"
nimbus_apt_private_init "$APT"
cat >"$APT/etc/sources.list.d/test.list" <<EOF
deb [trusted=yes] http://127.0.0.2/debian/ ./
deb [trusted=yes] http://127.0.0.1/nimbusos/ ./
deb [trusted=yes] http://127.0.0.3/docker/ ./
EOF
for pref in nimbusos.pref nimbusos-docker.pref; do
	cp "$PKG/apt/$pref" "$APT/etc/preferences.d/$pref"
	nimbus_render_tokens "$APT/etc/preferences.d/$pref"
done
APT_CONFIG="$APT/apt.conf" apt-get -q update >"$T/update.log" 2>&1 || { cat "$T/update.log"; exit 1; }
POLICY="$(APT_CONFIG="$APT/apt.conf" apt-cache policy bash nimbus-control docker-ce)"
echo
echo "$POLICY" | sed 's/^/    /'
echo
candidate() { awk -v p="$1:" '$1 == p { want = 1; next } want && $1 == "Candidate:" { print $2; exit }' <<<"$POLICY"; }
[[ "$(candidate bash)" == "5.2.37-2" ]] && pass "bash comes from Debian (NimbusOS 99.0 and Docker 98.0 are pinned to 1)" \
	|| fail "bash candidate is $(candidate bash), expected Debian's 5.2.37-2"
[[ "$(candidate nimbus-control)" == "0.2.0" ]] && pass "nimbus-control comes from the NimbusOS repository (priority 500)" \
	|| fail "nimbus-control candidate is $(candidate nimbus-control), expected 0.2.0"
[[ "$(candidate docker-ce)" == "29.0" ]] && pass "docker-ce comes from the Docker repository (priority 500)" \
	|| fail "docker-ce candidate is $(candidate docker-ce), expected 29.0"
grep -Eq '^ +99\.0 1$' <<<"$POLICY" && pass "NimbusOS's bash 99.0 has priority 1" || fail "NimbusOS's bash 99.0 is not at priority 1"

# --- nimbus-update's classifier on real apt output -----------------------------
echo "nimbus-update error classification on real apt output"
classify() {   # classify LOGFILE -> "source:kind ..." using nimbus-update's own code
	python3 - "$PKG/nimbus-update/nimbus-update" "$1" <<'PYEOF'
import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader("nu", sys.argv[1])
spec = importlib.util.spec_from_loader("nu", loader)
nu = importlib.util.module_from_spec(spec)
loader.exec_module(nu)
nu.APT_HOST = "127.0.0.1"
with open(sys.argv[2], encoding="utf-8") as handle:
    errors = nu.classify_apt_output(handle.readlines())
print(" ".join(f"{e['source']}:{e['kind']}" for e in errors))
PYEOF
}
check_apt_error() {   # check_apt_error NAME SOURCE-LINE EXPECTED
	local dir="$T/apt-$1"
	nimbus_apt_private_init "$dir"
	echo "$2" >"$dir/etc/sources.list.d/test.list"
	APT_CONFIG="$dir/apt.conf" LC_ALL=C.UTF-8 apt-get update >"$dir/out.log" 2>&1 || true
	local got
	got="$(classify "$dir/out.log")"
	if [[ " $got " == *" $3 "* ]]; then
		pass "$1: classified as $3"
	else
		fail "$1: expected $3, got '${got:-nothing}'. apt said:"
		sed 's/^/        /' "$dir/out.log"
	fi
}
check_apt_error "unsigned NimbusOS repository" "deb http://127.0.0.1/nimbusos/ ./" "nimbusos:signature"
check_apt_error "unresolvable host" "deb http://nimbusos-test.invalid/debian/ ./" "debian:dns"

echo
printf 'apt-integration.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
