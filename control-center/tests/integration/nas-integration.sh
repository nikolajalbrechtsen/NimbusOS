#!/bin/bash
# =============================================================================
# control-center/tests/integration/nas-integration.sh
# =============================================================================
# What: runs privd's storage and Samba verbs for real, from the source tree,
#       in a privileged throwaway container:
#         - three tmpfs mounts stand in for formatted disks (disk1, disk2,
#           parity1; each its own device, as SnapRAID needs). Formatting
#           itself isn't run here: a container can't make loop devices, and
#           the host's disks are visible (docs/TESTING.md tests it in QEMU);
#         - pool.apply: the mergerfs pool mounted from its fstab line, then
#           Pooled + parity with a real "snapraid sync" (nimbus-snapraid-run)
#           and snapraid.status's diff counts and unprotected bytes;
#         - SMB users through smbuser.add (adduser + smbpasswd on stdin);
#         - share.apply with the packaged nimbus.conf: testparm, smbd reload,
#           Avahi with _adisk, the isos share bound onto the ISO library;
#         - smbclient as alice, bob and a guest: Secure (guests read only),
#           Private (read-only for bob), Public (guests write), file modes and
#           group, the recycle bin, nimbus-recycle-clean, samba.test,
#           share.delete_data, deleting a user;
#         - nimbus-netfix against real iptables: rules added once, nothing
#           without a DOCKER-USER chain.
# Why:  docs/PLAN.md NOTE-68: the fakes in the unit tests follow the real
#       tools' behaviour; this checks that they do.
#
# Usage: control-center/tests/integration/nas-integration.sh
#        Root, in a privileged throwaway container only
#        (tools/dev-shell.sh --privileged). Network (deb.debian.org).
# =============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CC="$(dirname "$(dirname "$HERE")")"
[[ "$(id -u)" -eq 0 ]] || { echo "nas-integration: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] || { echo "nas-integration: throwaway container only" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends samba samba-vfs-modules smbclient mergerfs snapraid fuse3 iptables iproute2 \
	adduser passwd attr python3 procps >/tmp/nas-install.log 2>&1 || { tail -n 30 /tmp/nas-install.log; exit 1; }

# --- what the nimbus-control package sets up (sysusers, tmpfiles, postinst) --------------
addgroup --system nimbus-smb >/dev/null
adduser --system --no-create-home --home /nonexistent --shell /usr/sbin/nologin --ingroup nimbus-smb nimbus-guest >/dev/null
install -d /etc/samba/nimbus /var/lib/nimbus-privd /var/cache/nimbus-privd /var/lib/nimbus-control /etc/nimbus
install -d -m 2775 -g nimbus-smb /var/lib/nimbus-vms/isos
install -m 0644 "$CC/data/samba/nimbus.conf" /etc/samba/nimbus/nimbus.conf
: >/etc/samba/nimbus/settings.conf
: >/etc/samba/nimbus-shares.conf
printf '\n# BEGIN nimbus-control (docs/PLAN.md NOTE-23)\ninclude = /etc/samba/nimbus/nimbus.conf\n# END nimbus-control\n' >>/etc/samba/smb.conf
mkdir -p /run/samba
smbd -D
echo "debian 13 netinst" >/var/lib/nimbus-vms/isos/debian.iso

# --- three "disks" -------------------------------------------------------------------------
for d in /mnt/disks/disk1 /mnt/disks/disk2 /mnt/parity/parity1; do
	mkdir -p "$d"
	mount -t tmpfs -o size=6G tmpfs "$d"          # a limit, not memory: mergerfs wants 4 GiB free
done

cat >/tmp/privd.py <<'EOF'
# privd.py VERB [ARGS_JSON]: one privd verb with the server's argument checks.
import json, sys
sys.path[:0] = [f"{sys.argv[1]}/common", f"{sys.argv[1]}/privd"]
from nimbus_privd import samba, storage  # noqa: F401  (registers the verbs)
from nimbus_privd.registry import REGISTRY

class Ctx:
    def log(self, line):
        print(f"    | {line}", file=sys.stderr)

verb = REGISTRY.get(sys.argv[2])
args = REGISTRY.validate(verb, json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})
try:
    print(json.dumps(verb.handler(Ctx(), args)))
except Exception as exc:
    print(f"{type(exc).__name__}: {getattr(exc, 'message', exc)}", file=sys.stderr)
    sys.exit(1)
EOF
privd() { python3 /tmp/privd.py "$CC" "$@"; }
python3 - "$CC" <<'EOF'
import sys
sys.path[:0] = [f"{sys.argv[1]}/common", f"{sys.argv[1]}/privd"]
from nimbus_privd import fsutil
fsutil.write_block([{"kind": "disk", "role": "data", "label": "disk1", "uuid": "11111111-aaaa", "fs": "xfs"},
                    {"kind": "disk", "role": "data", "label": "disk2", "uuid": "22222222-bbbb", "fs": "xfs"},
                    {"kind": "disk", "role": "parity", "label": "parity1", "uuid": "33333333-cccc", "fs": "xfs"}])
EOF

echo "Pool and parity"
check "pool.apply pooled mounts mergerfs from its fstab line" privd pool.apply '{"mode": "pooled", "data": ["disk1", "disk2"]}'
check "/mnt/user is a mergerfs pool over disk1 and disk2" \
	sh -c '[ "$(findmnt -no FSTYPE /mnt/user)" = fuse.mergerfs ] && getfattr --absolute-names -n user.mergerfs.branches /mnt/user/.mergerfs | grep -q "/mnt/disks/disk1=RW:/mnt/disks/disk2=RW"'
check "Pooled + parity writes the SnapRAID configuration" privd pool.apply '{"mode": "parity", "data": ["disk1", "disk2"], "parity": ["parity1"]}'
check "the pool is still mounted (changed in place)" sh -c '[ "$(findmnt -no FSTYPE /mnt/user)" = fuse.mergerfs ]'

echo "SMB users"
check "smbuser.add alice" privd smbuser.add '{"user": "alice", "password": "Alice-pass-123"}'
check "smbuser.add bob" privd smbuser.add '{"user": "bob", "password": "Bob-pass-1234"}'
check "they are system accounts in nimbus-smb and Samba users" \
	sh -c 'id -nG bob | grep -qw nimbus-smb && [ "$(getent passwd bob | cut -d: -f7)" = /usr/sbin/nologin ] && pdbedit -L | grep -q "^bob:"'
check "a duplicate is refused" sh -c '! python3 /tmp/privd.py "$1" smbuser.add "{\"user\": \"bob\", \"password\": \"Bob-pass-1234\"}"' _ "$CC"

echo "Shares"
SHARES='[{"name": "media", "location": "pool", "security": "secure", "users": {"alice": "rw"}, "recycle": {"enabled": true, "days": 30}},
 {"name": "docs", "location": "pool", "security": "private", "users": {"alice": "rw", "bob": "ro"}},
 {"name": "drop", "location": "system", "security": "public"},
 {"name": "timemachine", "location": "pool", "security": "private", "users": {"alice": "rw"}, "timemachine": {"enabled": true, "max_gb": 100}},
 {"name": "isos", "location": "pool", "security": "secure", "users": {"alice": "rw"}}]'
SETTINGS='{"enabled": true, "workgroup": "WORKGROUP", "description": "NimbusOS test", "netbios": false}'
check "share.apply (testparm, swap, reload)" privd share.apply "{\"shares\": $SHARES, \"settings\": $SETTINGS}"
check "testparm: no unknown parameters, every share encrypted on request and with vfs_fruit" \
	sh -c 'testparm -s 2>&1 | grep -q "Loaded services file OK" && ! testparm -s 2>&1 | grep -q -E "Unknown parameter|use vfs_fruit, others" && ! testparm -s 2>/dev/null | grep -q "server smb encrypt = default"'
check "share folders are root:nimbus-smb 2775" sh -c '[ "$(stat -c "%U:%G %a" /mnt/user/media)" = "root:nimbus-smb 2775" ] && [ "$(stat -c "%a" /srv/nimbus/shares/drop)" = 2775 ]'
check "Avahi advertises SMB and Time Machine" grep -q "dk0=adVN=timemachine" /etc/avahi/services/smb.service
check "the isos share is the ISO library now, with its ISO moved in" \
	sh -c 'findmnt -no TARGET /var/lib/nimbus-vms/isos >/dev/null && [ -f /mnt/user/isos/debian.iso ] && [ -f /var/lib/nimbus-vms/isos/debian.iso ]'

echo hello >/tmp/hello.txt
check "alice writes to media (Secure)" sh -c 'smbclient //127.0.0.1/media -U alice%Alice-pass-123 -c "put /tmp/hello.txt hello.txt" >/dev/null 2>&1; [ -f /mnt/user/media/hello.txt ]'
check "her file is group nimbus-smb, 0664, on a pool disk" \
	sh -c '[ "$(stat -c "%G %a" /mnt/user/media/hello.txt)" = "nimbus-smb 664" ] && ls /mnt/disks/disk*/media/hello.txt'
check "a guest can read media" sh -c 'smbclient //127.0.0.1/media -N -c "get hello.txt /tmp/guest.txt" && cmp /tmp/hello.txt /tmp/guest.txt'
check "a guest can't write to media" sh -c 'smbclient //127.0.0.1/media -N -c "put /tmp/hello.txt guest.txt" 2>&1 | grep -q NT_STATUS_ACCESS_DENIED'
check "bob can read docs but not write (read-only)" sh -c '
	smbclient //127.0.0.1/docs -U alice%Alice-pass-123 -c "put /tmp/hello.txt a.txt" >/dev/null 2>&1 &&
	smbclient //127.0.0.1/docs -U bob%Bob-pass-1234 -c "get a.txt /tmp/bob.txt" >/dev/null 2>&1 &&
	smbclient //127.0.0.1/docs -U bob%Bob-pass-1234 -c "put /tmp/hello.txt b.txt" 2>&1 | grep -q NT_STATUS_ACCESS_DENIED'
check "a guest can't open docs (Private)" sh -c '! smbclient //127.0.0.1/docs -N -c "ls" >/dev/null 2>&1'
check "a guest can write to drop (Public)" sh -c 'smbclient //127.0.0.1/drop -N -c "put /tmp/hello.txt g.txt" >/dev/null 2>&1; [ -f /srv/nimbus/shares/drop/g.txt ]'
check "deleting in media goes to the recycle bin" sh -c '
	smbclient //127.0.0.1/media -U alice%Alice-pass-123 -c "del hello.txt" >/dev/null 2>&1 &&
	[ ! -e /mnt/user/media/hello.txt ] && find /mnt/user/media/.recycle -name hello.txt | grep -q .'
check "samba.test: alice sees her shares" sh -c 'python3 /tmp/privd.py "$1" samba.test "{\"user\": \"alice\", \"password\": \"Alice-pass-123\"}" | grep -q "\"ok\": true"' _ "$CC"
check "samba.test: a wrong password is reported as such" sh -c 'python3 /tmp/privd.py "$1" samba.test "{\"user\": \"alice\", \"password\": \"wrong-pass-123\"}" | grep -q "wrong user name or password"' _ "$CC"

echo "Recycle bin cleanup"
python3 - "$CC" "$SHARES" <<'EOF'
import json, sys
sys.path[:0] = [f"{sys.argv[1]}/common"]
from nimbus_common import shares as S
specs = [S.share_spec(s, "") for s in json.loads(sys.argv[2])]
json.dump({"shares": [{**s, "path": S.share_path(s)} for s in specs]}, open("/var/lib/nimbus-control/shares.json", "w"))
EOF
old="$(find /mnt/user/media/.recycle -name hello.txt | head -1)"
touch -d "40 days ago" "$old"
mkdir -p /mnt/user/media/.recycle/alice && echo new >/mnt/user/media/.recycle/alice/new.txt
check "nimbus-recycle-clean removes files past 30 days, keeps newer ones" \
	sh -c 'PYTHONPATH="$1/common" python3 "$1/bin/nimbus-recycle-clean" && [ ! -e "$2" ] && [ -f /mnt/user/media/.recycle/alice/new.txt ]' _ "$CC" "$old"

echo "Parity"
echo "film" >/mnt/user/media/film.mkv
check "nimbus-snapraid-run syncs (and scrubs the first time)" sh -c 'python3 "$1/bin/nimbus-snapraid-run" >/tmp/snapraid.log 2>&1' _ "$CC"
check "its status records a good sync" python3 -c 'import json; s = json.load(open("/var/cache/nimbus-privd/snapraid-status.json")); assert s["last_sync_ok"] and s["last_scrub_ok"]'
echo "another" >/mnt/user/media/new.mkv
echo "ab" >"/mnt/user/media/my film (1).mkv"         # snapraid escapes it: my\ film\ \(1\).mkv
check "snapraid.status counts the unprotected changes and their size (8 + 3 bytes)" sh -c 'python3 /tmp/privd.py "$1" snapraid.status | python3 -c "import json,sys; s=json.load(sys.stdin); assert s[\"diff\"][\"unprotected\"] >= 1 and s[\"diff\"][\"bytes\"] == 11, s"' _ "$CC"

echo "Removing"
check "share.delete_data refuses a share that is still exported" sh -c '! python3 /tmp/privd.py "$1" share.delete_data "{\"name\": \"drop\", \"location\": \"system\"}"' _ "$CC"
check "removing the drop and isos shares" privd share.apply "{\"shares\": $(python3 -c 'import json,sys; print(json.dumps([s for s in json.loads(sys.argv[1]) if s["name"] not in ("drop", "isos")]))' "$SHARES"), \"settings\": $SETTINGS}"
check "the ISO library is unbound again" sh -c '! findmnt -no TARGET /var/lib/nimbus-vms/isos'
check "share.delete_data deletes its folder" sh -c 'python3 /tmp/privd.py "$1" share.delete_data "{\"name\": \"drop\", \"location\": \"system\"}" && [ ! -e /srv/nimbus/shares/drop ]' _ "$CC"
check "smbuser.delete bob removes the account and the Samba user" sh -c 'python3 /tmp/privd.py "$1" smbuser.delete "{\"user\": \"bob\"}" && ! getent passwd bob && ! pdbedit -L | grep -q "^bob:"' _ "$CC"

echo "nimbus-netfix"
check "without Docker's chain it does nothing" sh -c '"$1/bin/nimbus-netfix" | grep -q "nothing to do"' _ "$CC"
iptables -N DOCKER-USER
ip link add br0 type bridge
check "with it, VM traffic is let through" "$CC/bin/nimbus-netfix"
check "running it again adds nothing twice" sh -c '"$1/bin/nimbus-netfix" >/dev/null && [ "$(iptables -S DOCKER-USER | grep -c -- "-j ACCEPT")" = 3 ] && iptables -C DOCKER-USER -i br0 -o br0 -j ACCEPT' _ "$CC"

echo
printf 'nas-integration: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0)) || { echo "--- iptables"; iptables -S DOCKER-USER 2>&1 | head; echo "--- snapraid log"; tail -n 15 /tmp/snapraid.log 2>/dev/null; exit 1; }
