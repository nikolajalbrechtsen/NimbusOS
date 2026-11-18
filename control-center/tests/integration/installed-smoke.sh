#!/bin/bash
# =============================================================================
# control-center/tests/integration/installed-smoke.sh
# =============================================================================
# What: installs the built nimbus-control (with nimbusos-release and the
#       keyring, the real nimbus-update) in a throwaway container and runs it
#       the way systemd would, without systemd:
#         - nimbus-privd through systemd-socket-activate on
#           /run/nimbus-privd.sock (0660 root:nimbus-control, as the unit);
#         - nimbus-migrate, then nimbus-control-server, as the nimbus-control
#           user (setpriv), on 127.0.0.1:7777.
#       Then checks, over HTTP and the socket:
#         - the token file (0640, group nimbus-control); the page, the fonts
#           from Debian's packages and the logo are served; the CSP is set;
#         - /api needs the token; the launch-code exchange works once;
#         - /api/system/info reads the installed os-release;
#         - /api/updates returns the real nimbus-update status through privd;
#           "Check now" runs apt and reports the unreachable NimbusOS
#           repository as a classified error, not as "no updates";
#         - a desktop user in the nimbus-control group can open the socket but
#           privd refuses them (SO_PEERCRED);
#         - privd audited each request.
# Why:  the pieces are unit-tested; this proves they fit together as packaged
#       (paths, Python module path, users, permissions).
#
# Usage: control-center/tests/integration/installed-smoke.sh DEBS_DIR
#        DEBS_DIR: nimbus-control, nimbusos-release, nimbusos-archive-keyring
#        and nimbusos-branding .debs. Network (deb.debian.org). Throwaway
#        container only.
# =============================================================================
set -Eeuo pipefail

DEBS="$(readlink -f "${1:?usage: installed-smoke.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "installed-smoke: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] || { echo "installed-smoke: throwaway container only" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
WORK="$(mktemp -d)"
pids=()
finish() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap finish EXIT

pick() { find "$DEBS" -maxdepth 1 -name "$1_*.deb" -print -quit; }
debs=("$(pick nimbus-control)" "$(pick nimbusos-release)" "$(pick nimbusos-archive-keyring)" "$(pick nimbusos-branding)")
for d in "${debs[@]}"; do [[ -n "$d" ]] || { echo "installed-smoke: a .deb is missing in $DEBS" >&2; exit 2; }; done

# Too large or not in Debian: empty stand-ins (they don't take part here).
for p in docker-ce docker-ce-cli docker-compose-plugin chromium qemu-system-x86 ovmf swtpm-tools kdialog; do
	mkdir -p "$WORK/$p/DEBIAN"
	printf 'Package: %s\nVersion: 99:0+stand.in\nArchitecture: all\nMaintainer: tests <tests@invalid>\nDescription: stand-in\n' "$p" >"$WORK/$p/DEBIAN/control"
	dpkg-deb -b --root-owner-group "$WORK/$p" "$WORK/$p.deb" >/dev/null && dpkg -i "$WORK/$p.deb" >/dev/null
done
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends "${debs[@]}" curl python3 >"$WORK/install.log" 2>&1 \
	|| { tail -n 30 "$WORK/install.log"; exit 1; }
useradd --create-home --groups nimbus-control alice

# --- start privd and the backend, as their units would --------------------------
systemd-socket-activate -l /run/nimbus-privd.sock /usr/lib/nimbus-control/privd/nimbus-privd.py \
	>"$WORK/privd.log" 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -S /run/nimbus-privd.sock ]] && break; sleep 0.1; done
chgrp nimbus-control /run/nimbus-privd.sock && chmod 0660 /run/nimbus-privd.sock
AS_SERVICE=(setpriv --reuid=nimbus-control --regid=nimbus-control --init-groups --)
"${AS_SERVICE[@]}" /usr/lib/nimbus-control/bin/nimbus-migrate
"${AS_SERVICE[@]}" /usr/lib/nimbus-control/bin/nimbus-control-server >"$WORK/server.log" 2>&1 &
pids+=($!)
for _ in $(seq 100); do curl -fs -o /dev/null http://127.0.0.1:7777/ && break; sleep 0.2; done

TOKEN="$(cat /run/nimbus-control/token)"
api() { curl -fsS -H "Authorization: Bearer $TOKEN" "$@"; }

echo "Service"
check "the token file is 0640 nimbus-control:nimbus-control" \
	test "$(stat -c '%a %U:%G' /run/nimbus-control/token)" = "640 nimbus-control:nimbus-control"
check "the page is served, with the CSP" sh -c 'curl -fsSi http://127.0.0.1:7777/ | grep -qi "^content-security-policy: default-src .self."'
check "Inter from fonts-inter is served" sh -c 'curl -fsS -o /dev/null -w "%{content_type}" http://127.0.0.1:7777/fonts/inter-400 | grep -qx font/otf'
check "JetBrains Mono from fonts-jetbrains-mono is served" curl -fsS -o /dev/null http://127.0.0.1:7777/fonts/mono-400
check "the NimbusOS logo (nimbusos-branding) is served" curl -fsS -o /dev/null http://127.0.0.1:7777/logo.svg
check "/api without the token: 401" sh -c '[ "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:7777/api/updates)" = 401 ]'
check "a foreign Host header: 403" sh -c '[ "$(curl -s -o /dev/null -w "%{http_code}" -H "Host: evil.example:7777" http://127.0.0.1:7777/)" = 403 ]'
code="$(api -X POST http://127.0.0.1:7777/api/session/launch-code | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')"
check "the launch code gives the token once" sh -c '
	t=$(curl -fsS -H "Content-Type: application/json" -d "{\"code\": \"$1\"}" http://127.0.0.1:7777/api/session/exchange) &&
	echo "$t" | grep -q "$2" &&
	[ "$(curl -s -o /dev/null -w "%{http_code}" -H "Content-Type: application/json" -d "{\"code\": \"$1\"}" http://127.0.0.1:7777/api/session/exchange)" = 403 ]' _ "$code" "$TOKEN"
check "system info reads the installed os-release" sh -c 'curl -fsS -H "Authorization: Bearer $1" http://127.0.0.1:7777/api/system/info | grep -q "\"name\":\"NimbusOS\""' _ "$TOKEN"

echo "Updates through privd and nimbus-update"
status="$(api http://127.0.0.1:7777/api/updates)"
check "status comes from nimbus-update (channel, policy, version)" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["channel"] in ("stable","beta") and s["policy"] and "installed" in s' "$status"
checked="$(api -X POST http://127.0.0.1:7777/api/updates/check 2>&1)" || checked="{\"http_error\": \"${checked//\"/}\", \"ok\": true, \"errors\": []}"
check "Check now reports the unreachable NimbusOS repository as an error" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert not s["ok"] and any(e["source"]=="nimbusos" and e["hint"] for e in s["errors"])' "$checked"
echo "    errors: $(python3 -c 'import json,sys; print([(e["source"], e["kind"]) for e in json.loads(sys.argv[1])["errors"]])' "$checked")"
check "a bad channel is refused before privd" sh -c '[ "$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "Authorization: Bearer $1" -H "Content-Type: application/json" -d "{\"channel\": \"x\"}" http://127.0.0.1:7777/api/updates/channel)" = 422 ]' _ "$TOKEN"

echo "privd"
cat >"$WORK/client.py" <<'EOF'
import json, socket
s = socket.socket(socket.AF_UNIX)
s.connect("/run/nimbus-privd.sock")
s.sendall(b'{"id":"x1","verb":"update.status","args":{}}\n')
print(s.makefile().readline())
EOF
chmod a+r "$WORK/client.py"
chmod a+rx "$WORK"
reply="$(setpriv --reuid=alice --regid=alice --init-groups -- python3 "$WORK/client.py" 2>&1 || true)"
check "a desktop user in nimbus-control can open the socket but is refused" grep -q '"code":"denied"' <<<"$reply"
check "privd audited the requests (and the refusal)" \
	sh -c 'grep -q "update.status requested by pid" "$1" && grep -q "update.check by pid .*: ok" "$1" && grep -q "refused a connection from uid" "$1"' _ "$WORK/privd.log"

echo
printf 'installed-smoke: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0)) || { echo "--- server log"; tail -n 20 "$WORK/server.log"; echo "--- privd log"; tail -n 20 "$WORK/privd.log"; exit 1; }
