#!/bin/bash
# =============================================================================
# tools/tests/test-mirror.test.sh
# =============================================================================
# What: runs tools/test-mirror.sh's repository commands against a real apt
#       client, the way the QEMU smoke test uses them:
#         - init makes a throwaway key (secret file 0600, the directory
#           ignored by git) and an empty repository that publish-apt.sh's
#           check accepts; env points builds at http://10.0.2.2:PORT/apt;
#         - serve answers on 127.0.0.1, and apt (Signed-By the throwaway
#           key) updates from it;
#         - break-signature makes apt fail, and nimbus-update's own
#           classifier calls it a "signature" error of the NimbusOS
#           repository: what the Updates page shows (smoke test 10);
#         - repair makes apt succeed again;
#         - bad tags, ports and arguments are refused.
#       publish and iso are not run here: they are build-debs.sh,
#       publish-apt.sh and build.sh, which have their own tests, and the
#       QEMU test in docs/TESTING.md runs them for real.
# Why:  the QEMU test relies on these commands; a broken one would only show
#       up after an hour-long ISO build.
#
# Usage: tools/tests/test-mirror.test.sh
#        Debian 13 as root (tools/dev-shell.sh); installs gpg, apt-utils,
#        dpkg-dev, xz-utils and python3 if missing. Binds a port on loopback.
#        No network outside the machine.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
TOOL="$ROOT/tools/test-mirror.sh"
[[ "$(id -u)" -eq 0 ]] || { echo "test-mirror.test: run as root (tools/dev-shell.sh)" >&2; exit 2; }
# shellcheck source=build/lib/common.sh
. "$ROOT/build/lib/common.sh"
nimbus_load_conf
nimbus_apt_install_missing gpg gpg-agent gpgv apt-utils dpkg-dev xz-utils python3
nimbus_mktemp_dir T
M="$T/mirror"
PORT=18080

passed=0
failed=0
pass() { passed=$((passed + 1)); printf '  PASS  %s\n' "$1"; }
fail() { failed=$((failed + 1)); printf '  FAIL  %s\n' "$1"; }
check() {   # check NAME COMMAND...
	local name="$1"
	shift
	if "$@"; then pass "$name"; else fail "$name"; fi
}
refuses() {   # refuses NAME MESSAGE-PART ARGS...: test-mirror.sh ARGS must fail saying MESSAGE-PART
	local name="$1" want="$2" out rc=0
	shift 2
	out="$("$TOOL" --dir "$M" "$@" 2>&1)" || rc=$?
	if ((rc != 0)) && [[ "$out" == *"$want"* ]]; then
		pass "$name"
	else
		fail "$name (exit status $rc): $out"
	fi
}

echo "init and env"
"$TOOL" --dir "$M" --port "$PORT" init >"$T/init.log" 2>&1 || { cat "$T/init.log"; fail "init"; }
check "init: secret key file, mode 0600" test "$(stat -c %a "$M/key/secret.asc" 2>/dev/null)" = 600
check "init: one public key" test "$(nimbus_key_fingerprints "$M/key/public/test-mirror.asc" | wc -l)" -eq 1
check "init: the directory is ignored by git" grep -qx '\*' "$M/.gitignore"
check "init: both suites are signed" test -s "$M/site/apt/dists/stable/InRelease" -a -s "$M/site/apt/dists/beta/InRelease"
check "init: releases.json is signed" test -s "$M/site/apt/releases.json.sig"
fpr="$(nimbus_key_fingerprints "$M/key/public/test-mirror.asc")"
"$TOOL" --dir "$M" --port "$PORT" init >"$T/init2.log" 2>&1 || fail "init again"
check "init again: keeps the key" test "$(nimbus_key_fingerprints "$M/key/public/test-mirror.asc")" = "$fpr"
env_out="$("$TOOL" --dir "$M" --port 9090 env)"
check "env: the URL QEMU guests use" grep -qx "export NIMBUS_APT_URL=http://10.0.2.2:9090/apt" <<<"$env_out"
check "env: the pin host has no port" grep -qx "export NIMBUS_APT_HOST=10.0.2.2" <<<"$env_out"
check "env: releases.json on the mirror" grep -qx "export NIMBUS_RELEASES_JSON_URL=http://10.0.2.2:9090/apt/releases.json" <<<"$env_out"
check "env: the throwaway key directory" grep -qx "export NIMBUS_ARCHIVE_KEYS_DIR=$M/key/public" <<<"$env_out"
check "env: a test ISO name" grep -qx "export NIMBUS_ISO_BASENAME=${NIMBUS_ID}-test-${NIMBUS_ARCH}" <<<"$env_out"

echo "serve, and a real apt client"
"$TOOL" --dir "$M" --port "$PORT" serve >"$T/serve.log" 2>&1 &
for _ in $(seq 50); do
	if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then break; fi
	sleep 0.1
done
A="$T/apt"
nimbus_apt_private_init "$A"
nimbus_dearmor_into "$A/test-mirror.gpg" "$M/key/public/test-mirror.asc"
cat >"$A/etc/sources.list.d/nimbusos.sources" <<EOF
Types: deb
URIs: http://127.0.0.1:${PORT}/apt/
Suites: stable beta
Components: ${NIMBUS_APT_COMPONENT}
Signed-By: ${A}/test-mirror.gpg
EOF
apt_update() {   # apt_update LOG: apt-get update's exit status, output in LOG
	rm -rf -- "$A/state/lists"
	mkdir -p -- "$A/state/lists/partial"
	APT_CONFIG="$A/apt.conf" LC_ALL=C.UTF-8 apt-get update >"$1" 2>&1
}
classify() {   # classify LOG: "source:kind ..." from nimbus-update's own code
	python3 - "$ROOT/packages/nimbusos-release/nimbus-update/nimbus-update" "$1" <<'PYEOF'
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
if apt_update "$T/apt-ok.log"; then
	pass "apt-get update from the fresh mirror succeeds"
else
	fail "apt-get update from the fresh mirror failed:"
	sed 's/^/        /' "$T/apt-ok.log"
fi

echo "break-signature and repair (smoke test 10)"
"$TOOL" --dir "$M" --port "$PORT" break-signature >"$T/break.log" 2>&1 || { cat "$T/break.log"; fail "break-signature"; }
if apt_update "$T/apt-bad.log"; then
	fail "apt-get update succeeded with a broken InRelease"
else
	pass "apt-get update fails with a broken InRelease"
fi
got="$(classify "$T/apt-bad.log")"
if [[ "$got" == "nimbusos:signature" ]]; then
	pass "nimbus-update reports one error: nimbusos:signature"
else
	fail "nimbus-update classified it as '${got:-nothing}'. apt said:"
	sed 's/^/        /' "$T/apt-bad.log"
fi
refuses "break-signature twice is refused" "has no Date: line" break-signature
"$TOOL" --dir "$M" --port "$PORT" repair >"$T/repair.log" 2>&1 || { cat "$T/repair.log"; fail "repair"; }
if apt_update "$T/apt-repaired.log"; then
	pass "after repair, apt-get update succeeds again"
else
	fail "after repair, apt-get update still fails:"
	sed 's/^/        /' "$T/apt-repaired.log"
fi
"$TOOL" --dir "$M" --port "$PORT" break-signature beta >"$T/break-beta.log" 2>&1 || fail "break-signature beta"
apt_update "$T/apt-bad-beta.log" || true
got="$(classify "$T/apt-bad-beta.log")"
if grep -Eq "^(Err|E):.* beta " "$T/apt-bad-beta.log" && ! grep -Eq "^(Err|E):.* stable " "$T/apt-bad-beta.log" \
	&& [[ "$got" == "nimbusos:signature" ]]; then
	pass "break-signature beta: only the beta suite fails, as nimbusos:signature"
else
	fail "break-signature beta: expected only beta to fail as nimbusos:signature, got '${got:-nothing}'. apt said:"
	sed 's/^/        /' "$T/apt-bad-beta.log"
fi
"$TOOL" --dir "$M" --port "$PORT" repair >"$T/repair2.log" 2>&1 || fail "repair after beta"
check "check passes after repair" "$TOOL" --dir "$M" check

echo "refusals"
refuses "publish: not a release tag" "is not a release tag" publish v1.0
refuses "publish: a dev version is not a tag" "is not a release tag" publish 0.1.0
refuses "iso: needs published packages" "No packages for v9.9.9" iso v9.9.9
refuses "--port below 1024" "--port must be" --port 80 env
refuses "--port not a number" "--port must be" --port 80a env
refuses "unknown command" "Unknown command" frobnicate
refuses "init takes no arguments" "init takes no arguments" init extra
refuses "break-signature of a missing suite" "does not exist" break-signature nonesuch
check "no secret key under the repository" test -z "$(git -C "$ROOT" ls-files -- '*secret*' 'dist/*')"

echo
printf 'test-mirror.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
