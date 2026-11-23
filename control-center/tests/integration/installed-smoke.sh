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
#         - the Apps API answers without Docker running and without
#           nimbus-catalog: an empty catalog, "Docker isn't running"; then,
#           with nimbus-catalog installed and no restart, its ten apps and
#           their icons, and a 1-click plan with this computer's name and
#           the nimbus-smb gid;
#         - privd's stack verbs, called by the service user, hand a folder
#           in /var/lib/nimbus-control/stacks to a container's uid and
#           delete a stack folder holding files the service user can't touch;
#         - the VM API answers "libvirt isn't running" without failing; noVNC
#           and the console page are served; a chunked ISO upload lands in
#           /var/lib/nimbus-vms/isos as nimbus-control:nimbus-smb 0644 and
#           can be deleted; websockify, started as its unit starts it, finds
#           a token file in /run/nimbus-control/vnc-tokens and proxies to the
#           VNC target, and refuses an unknown token;
#         - a desktop user in the nimbus-control group can open the socket but
#           privd refuses them (SO_PEERCRED);
#         - NAS: the storage overview through privd; an SMB user and a
#           Private share made through the API (privd as root, with the
#           packaged Samba drop-in and the include postinst added); its
#           folder's owner and mode; testparm; the Avahi file; smbd serving
#           it to that user (files group nimbus-smb, 0664) and not to guests;
#           Test connection; the default settings;
#         - the Dashboard: its files; /api/dashboard with real psutil numbers,
#           Docker and libvirt reported down; /ws/metrics every 2 seconds;
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
CATALOG_DEB="$(pick nimbus-catalog)"
[[ -n "$CATALOG_DEB" ]] || { echo "installed-smoke: nimbus-catalog's .deb is missing in $DEBS" >&2; exit 2; }

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

echo "Apps"
check "the Apps page is served" curl -fsS -o /dev/null http://127.0.0.1:7777/views/apps.js
apps="$(api http://127.0.0.1:7777/api/apps)"
check "the apps list says Docker isn't running, and doesn't fail" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["apps"] == [] and not s["docker"]["ok"] and "systemctl start docker" in s["docker"]["message"]' "$apps"
check "the catalog is empty without nimbus-catalog" \
	python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == {"apps": [], "errors": []}' "$(api http://127.0.0.1:7777/api/catalog)"
dpkg -i "$CATALOG_DEB" >/dev/null
check "with nimbus-catalog installed, the running service lists its ten apps" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert len(s["apps"]) == 10 and s["errors"] == [] and all(a["icon"] for a in s["apps"])' \
	"$(api http://127.0.0.1:7777/api/catalog)"
check "a catalog icon is served" sh -c 'curl -fsS http://127.0.0.1:7777/catalog-icons/jellyfin.svg | grep -q "<svg"'
check "a 1-click plan fills in this computer's name and the shares' group (Syncthing)" \
	python3 -c 'import json,sys,yaml; s=yaml.safe_load(json.loads(sys.argv[1])["compose"])["services"]["syncthing"]; assert s["environment"]["PGID"] == sys.argv[2] and s["hostname"] == sys.argv[3], s' \
	"$(api -X POST -H 'Content-Type: application/json' -d '{"app": "syncthing"}' http://127.0.0.1:7777/api/apps/plan)" \
	"$(getent group nimbus-smb | cut -d: -f3)" "$(hostname)"

echo "Virtual machines"
for page in views/vms.js console.html console.js lib/vnc.js novnc/core/rfb.js; do
	check "/$page is served" curl -fsS -o /dev/null "http://127.0.0.1:7777/$page"
done
vms="$(api http://127.0.0.1:7777/api/vms || true)"
check "the VM list says libvirt isn't running, and doesn't fail" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["vms"] == [] and not s["libvirt"]["ok"] and "libvirtd" in s["libvirt"]["message"]' "$vms"
ISOS=/var/lib/nimbus-vms/isos
head -c 3145728 /dev/urandom >"$WORK/smoke.iso"
begin="$(api -X POST -H 'Content-Type: application/json' -d '{"name": "smoke.iso", "size": 3145728}' \
	http://127.0.0.1:7777/api/isos/uploads || true)"
uid="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$begin" 2>/dev/null || true)"
api -X PUT -H 'Content-Type: application/octet-stream' --data-binary @"$WORK/smoke.iso" \
	"http://127.0.0.1:7777/api/isos/uploads/$uid?offset=0" >/dev/null || true
api -X POST "http://127.0.0.1:7777/api/isos/uploads/$uid/finish" >/dev/null || true
check "an uploaded ISO is nimbus-control:nimbus-smb 0644 in $ISOS" \
	test "$(stat -c '%U:%G %a' "$ISOS/smoke.iso" 2>&1)" = "nimbus-control:nimbus-smb 644"
check "it arrived byte for byte, with no unfinished upload left" \
	sh -c 'cmp -s "$1" "$2/smoke.iso" && [ -z "$(find "$2" -name ".upload-*")" ]' _ "$WORK/smoke.iso" "$ISOS"
check "the ISO library lists it" sh -c 'curl -fsS -H "Authorization: Bearer $1" http://127.0.0.1:7777/api/isos | grep -q "\"smoke.iso\""' _ "$TOKEN"
api -X DELETE http://127.0.0.1:7777/api/isos/smoke.iso >/dev/null || true
check "deleting it removes the file" test ! -e "$ISOS/smoke.iso"
# websockify exactly as nimbus-websockify.service starts it, and a VNC stand-in.
"${AS_SERVICE[@]}" /usr/bin/websockify --token-plugin=TokenFile --token-source=/run/nimbus-control/vnc-tokens \
	127.0.0.1:6080 >"$WORK/websockify.log" 2>&1 &
pids+=($!)
python3 -c '
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 5999)); s.listen()
held = []
while True:
    c, _ = s.accept(); c.sendall(b"RFB 003.008\n"); held.append(c)' &
pids+=($!)
for _ in $(seq 50); do python3 -c 'import socket; socket.create_connection(("127.0.0.1", 6080), 1)' 2>/dev/null && break; sleep 0.2; done
echo "smoke-console-token-0123456789: 127.0.0.1:5999" | "${AS_SERVICE[@]}" tee /run/nimbus-control/vnc-tokens/smoke >/dev/null
check "websockify (as nimbus-control) proxies a console token to its VNC server" python3 -c '
import websockets.sync.client as c
with c.connect("ws://127.0.0.1:6080/websockify?token=smoke-console-token-0123456789", subprotocols=["binary"]) as ws:
    assert ws.recv(timeout=10) == b"RFB 003.008\n"'
check "websockify refuses an unknown token" python3 -c '
import websockets.sync.client as c
try:
    with c.connect("ws://127.0.0.1:6080/websockify?token=guess", subprotocols=["binary"]) as ws:
        ws.recv(timeout=5)
except Exception:
    raise SystemExit(0)
raise SystemExit(1)'

echo "Dashboard"
for page in views/dashboard.js lib/charts.js; do
	check "/$page is served" curl -fsS -o /dev/null "http://127.0.0.1:7777/$page"
done
check "/api/dashboard measures the host (real psutil) and says Docker and libvirt are down" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); h=s["host"]; assert h["memory"]["total"] > 0 and 0 <= h["cpu"] <= 100 and len(s["history"]["cpu"]) >= 1 and h["threads"] >= 1; assert not s["docker"]["ok"] and not s["libvirt"]["ok"]; assert s["storage"]["system"]["total"] > 0 and isinstance(s["alerts"], list)' \
	"$(api http://127.0.0.1:7777/api/dashboard || echo '{}')"
check "/ws/metrics pushes a new sample every 2 seconds" python3 - "$TOKEN" <<'EOF'
import json, sys, time, websockets.sync.client as c
with c.connect("ws://127.0.0.1:7777/ws/metrics", subprotocols=["nimbus.v1", f"nimbus.token.{sys.argv[1]}"],
               origin="http://127.0.0.1:7777") as ws:
    first = json.loads(ws.recv(timeout=10)); start = time.monotonic()
    second = json.loads(ws.recv(timeout=10)); gap = time.monotonic() - start
assert first["type"] == second["type"] == "tick" and second["t"] > first["t"] and 1.0 < gap < 4.0, gap
EOF

echo "NAS"
job_ok() {   # job_ok JSON_WITH_JOB_ID: wait for the job through the API, succeed if it did
	python3 - "$1" "$TOKEN" <<'EOF'
import json, sys, time, urllib.request
job = json.loads(sys.argv[1])["job"]
for _ in range(300):
    req = urllib.request.Request(f"http://127.0.0.1:7777/api/jobs/{job}", headers={"Authorization": f"Bearer {sys.argv[2]}"})
    state = json.load(urllib.request.urlopen(req))
    if state["state"] != "running":
        print(state["message"])
        sys.exit(0 if state["state"] == "ok" else 1)
    time.sleep(0.2)
sys.exit(1)
EOF
}
check "the storage overview answers (disks through privd, not the live session)" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["live"] is False and isinstance(s["disks"], list) and s["pool"]["mode"] == "single"' \
	"$(api http://127.0.0.1:7777/api/storage || echo '{}')"
check "an SMB user is added through privd" \
	sh -c 'curl -fsS -H "Authorization: Bearer $1" -H "Content-Type: application/json" -d "{\"name\": \"smoke\", \"password\": \"Smoke-pass-123\"}" http://127.0.0.1:7777/api/smbusers | grep -q added' _ "$TOKEN"
share_job="$(api -H 'Content-Type: application/json' \
	-d '{"name": "smoke", "location": "system", "security": "private", "users": {"smoke": "rw"}, "recycle": {"enabled": true, "days": 7}}' \
	http://127.0.0.1:7777/api/shares || echo '{"job": "none"}')"
check "a share is created (privd renders, checks with testparm and swaps in Samba's files)" job_ok "$share_job"
check "its folder is root:nimbus-smb 2775" test "$(stat -c '%U:%G %a' /srv/nimbus/shares/smoke 2>&1)" = "root:nimbus-smb 2775"
check "smb.conf reaches it through the packaged include" sh -c 'testparm -s 2>/dev/null | grep -q "^\[smoke\]" && ! testparm -s 2>&1 | grep -q -E "Unknown parameter|use vfs_fruit, others"'
check "Avahi advertises SMB, marked as generated" grep -q "Generated by nimbus-control" /etc/avahi/services/smb.service
mkdir -p /run/samba
smbd -D
for _ in $(seq 50); do python3 -c 'import socket; socket.create_connection(("127.0.0.1", 445), 1)' 2>/dev/null && break; sleep 0.2; done
echo "smoke" >"$WORK/hello.txt"
check "the user writes to the share over SMB, as group nimbus-smb with mode 0664" sh -c '
	smbclient //127.0.0.1/smoke -U smoke%Smoke-pass-123 -c "put $1 hello.txt" >/dev/null 2>&1 &&
	[ "$(stat -c "%G %a" /srv/nimbus/shares/smoke/hello.txt)" = "nimbus-smb 664" ]' _ "$WORK/hello.txt"
check "a guest can't open a Private share" sh -c '! smbclient //127.0.0.1/smoke -N -c ls >/dev/null 2>&1'
check "Test connection succeeds through the API" \
	sh -c 'curl -fsS -H "Authorization: Bearer $1" -H "Content-Type: application/json" -d "{\"user\": \"smoke\", \"password\": \"Smoke-pass-123\"}" http://127.0.0.1:7777/api/smbusers/test | grep -q "\"ok\":true"' _ "$TOKEN"
check "settings default to WORKGROUP and the temperature limits" \
	python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["samba"]["workgroup"] == "WORKGROUP" and s["alerts"] == {"hdd_temp_c": 50, "nvme_temp_c": 70}' \
	"$(api http://127.0.0.1:7777/api/settings)"
pkill -x smbd || true

echo "privd"
# client.py [VERB ARGS_JSON]: one request, prints the reply line.
cat >"$WORK/client.py" <<'EOF'
import json, socket, sys
verb, args = (sys.argv[1], json.loads(sys.argv[2])) if len(sys.argv) > 2 else ("update.status", {})
s = socket.socket(socket.AF_UNIX)
s.connect("/run/nimbus-privd.sock")
s.sendall((json.dumps({"id": "x1", "verb": verb, "args": args}) + "\n").encode())
print(s.makefile().readline())
EOF
chmod a+r "$WORK/client.py"
chmod a+rx "$WORK"
reply="$(setpriv --reuid=alice --regid=alice --init-groups -- python3 "$WORK/client.py" 2>&1 || true)"
check "a desktop user in nimbus-control can open the socket but is refused" grep -q '"code":"denied"' <<<"$reply"

STACKS=/var/lib/nimbus-control/stacks
"${AS_SERVICE[@]}" mkdir "$STACKS/smoke"
reply="$("${AS_SERVICE[@]}" python3 "$WORK/client.py" stack.prepare_volume \
	'{"stack": "smoke", "path": "config", "uid": 472, "gid": 472}' 2>&1 || true)"
check "stack.prepare_volume hands the folder to the container's uid" \
	python3 -c 'import json,os,sys; r=json.loads(sys.argv[1]); st=os.stat(sys.argv[2]); assert r["ok"] and (st.st_uid, st.st_gid) == (472, 472)' \
	"$reply" "$STACKS/smoke/config"
install -o 472 -g 472 -m 0600 /dev/null "$STACKS/smoke/config/database.db"
"${AS_SERVICE[@]}" rm -rf "$STACKS/smoke" 2>/dev/null || true
check "the service user can't delete what a container wrote" test -f "$STACKS/smoke/config/database.db"
reply="$("${AS_SERVICE[@]}" python3 "$WORK/client.py" stack.delete_data '{"stack": "smoke"}' 2>&1 || true)"
check "stack.delete_data removes the whole stack folder" \
	python3 -c 'import json,os,sys; r=json.loads(sys.argv[1]); assert r["ok"] and not os.path.exists(sys.argv[2] + "/smoke") and os.path.isdir(sys.argv[2])' \
	"$reply" "$STACKS"
check "privd audited the requests (and the refusal)" \
	sh -c 'grep -q "update.status requested by pid" "$1" && grep -q "update.check by pid .*: ok" "$1" && grep -q "refused a connection from uid" "$1"' _ "$WORK/privd.log"

echo
printf 'installed-smoke: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0)) || { echo "--- server log"; tail -n 20 "$WORK/server.log"; echo "--- privd log"; tail -n 20 "$WORK/privd.log"; exit 1; }
