#!/bin/bash
# =============================================================================
# packages/nimbus-control/tests/maintscripts.test.sh
# =============================================================================
# What: installs, upgrades, removes and purges the built nimbus-control
#       package in the current (throwaway) Debian container and checks what
#       its maintainer scripts do. The container is like the image build
#       chroot: systemd isn't running and policy-rc.d forbids starting
#       services (exit 101, as live-build does).
#         0. Depends: every dependency resolves in Trixie plus Docker's
#            repository (apt-get -s, network).
#         1. Static: the maintainer scripts never mention docker, virsh or
#            smbcontrol and never start anything except via deb-systemd-invoke.
#         2. First install of a fake older version (the same package,
#            repacked as 0.0.0~fake.1 with one changed program file):
#            accounts and groups from sysusers.d, directories and modes from
#            tmpfiles.d, the smb.conf include once and a valid testparm,
#            adduser.conf keys, nmbd off, libvirt's default network on
#            autostart, units enabled, and nimbus-migrate (the unit's
#            ExecStartPre, run as nimbus-control) creating schema 1.
#         3. Upgrade to the package under test: an administrator's later
#            choices (nmbd back on, wsdd2 off, own EXTRA_GROUPS) survive;
#            nimbus-control is restarted because its files changed, and a
#            reinstall with unchanged files only "starts" it; user data and
#            state are unchanged. The installed nimbus-migrate then runs a
#            step 2 as the service user (backup, schema 2), a failing step 3
#            (state restored, one clear message), and refuses a downgrade.
#         4. Remove keeps everything. Purge keeps every piece of user data
#            byte for byte (stacks with container-owned files, VM disks and
#            ISOs, pool, disks, share folders, SMB users with passwords,
#            the fstab block, SnapRAID config, privd's records), prints
#            where it is, removes only the Control Center's own state,
#            caches and Samba configuration, gives smb.conf back
#            byte-identical, and restores adduser.conf except where the
#            administrator changed it.
#       Throughout, wrappers record every deb-systemd-invoke, systemctl,
#       invoke-rc.d, service, docker, virsh and smbcontrol call that a
#       nimbus-control maintainer script makes.
#
# Why:  docs/PLAN.md NOTE-58 (purge never deletes user data), NOTE-59
#       (migrations), NOTE-61 (maintainer scripts in the chroot).
#
# Usage: packages/nimbus-control/tests/maintscripts.test.sh DEBS_DIR
#        Network (deb.debian.org, download.docker.com). Throwaway container
#        only (tools/dev-shell.sh): it installs packages and users.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd -P)"
DEBS="$(readlink -f "${1:?usage: maintscripts.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "maintscripts.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "maintscripts.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}

deb="$(find "$DEBS" -maxdepth 1 -name 'nimbus-control_*.deb' -print -quit)"
[[ -n "$deb" ]] || { echo "maintscripts.test: no nimbus-control .deb in $DEBS" >&2; exit 2; }
WORK="$(mktemp -d)"
LOG=/tmp/nimbus-control-calls.log
STATE=/var/lib/nimbus-control
[[ ! -e "$STATE" ]] || { echo "maintscripts.test: $STATE already exists; run this test in a fresh container" >&2; exit 2; }
SVC_RUN=(setpriv --reuid=nimbus-control --regid=nimbus-control --init-groups --)

# Dependencies too large for a test, or only in Docker's repository; they
# are replaced by empty stand-in packages after the resolvability check.
STAND_INS="docker-ce docker-ce-cli docker-compose-plugin chromium qemu-system-x86 ovmf swtpm-tools kdialog nimbusos-branding"

# --- helpers -------------------------------------------------------------------
stand_in() {   # stand_in NAME: build and install an empty package NAME
	local d="$WORK/stand-in-$1"
	mkdir -p "$d/DEBIAN"
	printf 'Package: %s\nVersion: 99:0+stand.in\nArchitecture: all\nMaintainer: NimbusOS tests <tests@invalid>\nDescription: stand-in for %s (nimbus-control tests)\n' "$1" "$1" >"$d/DEBIAN/control"
	dpkg-deb -b --root-owner-group "$d" "$WORK/$1.deb" >/dev/null
	dpkg -i "$WORK/$1.deb" >/dev/null
}
wrap() {   # wrap NAME [REAL]: log calls from nimbus-control's scripts, then run REAL
	# Calls made by deb-systemd-invoke/-helper themselves are not logged:
	# they are the sanctioned path.
	local run="exit 0"
	if [[ -n "${2:-}" ]]; then run="exec $2 \"\$@\""; fi
	cat >"/usr/local/bin/$1" <<EOF
#!/bin/sh
if [ "\${DPKG_MAINTSCRIPT_PACKAGE:-}" = nimbus-control ]; then
	case "\$(tr '\\0' ' ' </proc/\$PPID/cmdline 2>/dev/null)" in
		*deb-systemd-*) ;;
		*) echo "\${DPKG_MAINTSCRIPT_NAME:-?} $1 \$*" >>$LOG ;;
	esac
fi
$run
EOF
	chmod 0755 "/usr/local/bin/$1"
}
calls() { cat "$LOG" 2>/dev/null || true; }
called() { calls | grep -Eq -- "$1"; }
mode_owner() { [[ "$(stat -c '%a %U:%G' "$1")" == "$2" ]]; }
in_group() { id -nG "$1" | tr ' ' '\n' | grep -qx "$2"; }
smb_param() { testparm -s --parameter-name="$1" 2>/dev/null; }
is_enabled() { systemctl is-enabled "$1" 2>/dev/null | grep -qx enabled; }
digest() {    # digest: checksums of all user data (paths relative to /)
	(cd / && find var/lib/nimbus-control/stacks var/lib/nimbus-vms mnt srv/nimbus etc/nimbus \
		var/lib/nimbus-privd -type f ! -path 'var/lib/nimbus-privd/package/*' -print0 \
		| LC_ALL=C sort -z | xargs -0 sha256sum
	 stat -c '%n %a %u:%g' var/lib/nimbus-control/stacks/demo/data/grafana.db
	 sed -n '/^# BEGIN nimbus$/,/^# END nimbus$/p' etc/fstab
	 getent passwd alice nimbus-control nimbus-guest
	 getent group nimbus-smb nimbus-control
	 pdbedit -L 2>/dev/null | cut -d: -f1)
}
maintscripts_text() {   # the maintainer scripts of a .deb, comments removed
	local d
	d="$WORK/ctl-$(basename "$1")"
	mkdir -p "$d" && dpkg-deb -e "$1" "$d"
	for s in preinst postinst prerm postrm; do
		[[ -f "$d/$s" ]] && sed -e 's/#.*$//' "$d/$s"
	done
}

# --- 0. every dependency resolves ------------------------------------------------
echo "Dependencies"
apt-get update -q >/dev/null
. "$ROOT/build/lib/common.sh"
nimbus_load_conf
gpg --batch --yes --dearmor -o "$NIMBUS_DOCKER_KEYRING_PATH" "$ROOT/$NIMBUS_DOCKER_KEY_FILE"
cp "$ROOT/build/config/archives/docker.list.binary" /etc/apt/sources.list.d/docker.list
nimbus_render_tokens /etc/apt/sources.list.d/docker.list
apt-get update -q >/dev/null
# nimbusos-branding comes from NimbusOS itself: offer the built one.
branding_deb="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-branding_*.deb' -print -quit)"
[[ -n "$branding_deb" ]] || { echo "maintscripts.test: no nimbusos-branding .deb in $DEBS (a Depends of nimbus-control)" >&2; exit 2; }
check "all Depends and Recommends resolve in trixie + Docker's repository (+ nimbusos-branding)" \
	apt-get install -s -y --install-recommends "$deb" "$branding_deb"
rm -f /etc/apt/sources.list.d/docker.list
apt-get update -q >/dev/null

# --- 1. static ---------------------------------------------------------------------
echo "Maintainer scripts (static)"
scripts="$(maintscripts_text "$deb")"
# A command word: at the start of a line, after ; & | ( or $(, or after
# then/do/else/exec/!, optionally with a directory in front.
cmd='(^|[;&|(]|\$\(|[[:space:]](then|do|else|exec|!))[[:space:]]*([^[:space:]]*/)?'
check "no docker, virsh or smbcontrol command" \
	sh -c '! printf "%s\n" "$1" | grep -Eq "$2(docker|virsh|smbcontrol)([[:space:]]|\$)"' _ "$scripts" "$cmd"
check "no systemctl start/stop/restart/reload, invoke-rc.d or service command" \
	sh -c '! printf "%s\n" "$1" | grep -Eq "$2(systemctl([[:space:]]+-[^[:space:]]+)*[[:space:]]+(start|stop|restart|reload|try-restart|reload-or-restart)([[:space:]]|\$)|invoke-rc\.d|service[[:space:]])"' _ "$scripts" "$cmd"
check "the static check catches a direct call (self-test)" \
	sh -c 'printf "%s\n" "	if true; then /usr/bin/virsh net-start default; fi" "x=\$(systemctl --quiet restart foo)" \
		| grep -Ec "$1(virsh|systemctl([[:space:]]+-[^[:space:]]+)*[[:space:]]+restart)" | grep -qx 2' _ "$cmd"

# --- install the dependencies first, so only nimbus-control's calls are logged ----
for p in $STAND_INS; do stand_in "$p"; done
# One name per dependency: the first alternative, without version or ":any".
deps="$(dpkg-deb -f "$deb" Depends | tr ',' '\n' | sed -E 's/\|.*//; s/\(.*\)//; s/:any//; s/^[[:space:]]+|[[:space:]]+$//g' | grep -v '^$')"
# shellcheck disable=SC2086
apt-get install -y -q --no-install-recommends $deps python3-pytest >/tmp/deps.log 2>&1 \
	|| { tail -n 30 /tmp/deps.log; exit 1; }
cp -p /etc/samba/smb.conf "$WORK/smb.conf.before"
cp -p /etc/adduser.conf "$WORK/adduser.conf.before"
printf '#!/bin/sh\necho "policy-rc.d $*" >>%s\nexit 101\n' /tmp/policy-rc.d.log >/usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d
wrap deb-systemd-invoke /usr/bin/deb-systemd-invoke
wrap systemctl /usr/bin/systemctl
for c in invoke-rc.d service docker virsh smbcontrol; do wrap "$c"; done

# --- 2. first install of a fake older version ---------------------------------------
old="$WORK/old"
dpkg-deb -R "$deb" "$old"
sed -i -E 's/^Version: .*/Version: 0.0.0~fake.1/' "$old/DEBIAN/control"
echo "# changed in the fake older version" >>"$old/usr/lib/nimbus-control/migrations/0001_initial.py"
(cd "$old" && find usr etc -type f ! -path 'etc/*' -print0 | LC_ALL=C sort -z | xargs -0 md5sum >DEBIAN/md5sums)
dpkg-deb -b --root-owner-group "$old" "$WORK/nimbus-control_0.0.0~fake.1_all.deb" >/dev/null

echo "First install (fake older version 0.0.0~fake.1)"
: >"$LOG"
dpkg -i "$WORK/nimbus-control_0.0.0~fake.1_all.deb" >/tmp/install-old.log 2>&1 || { cat /tmp/install-old.log; exit 1; }
check "user nimbus-control: system uid, nologin, home /var/lib/nimbus-control" \
	sh -c 'e=$(getent passwd nimbus-control) && u=$(echo "$e" | cut -d: -f3) && [ "$u" -lt 1000 ] && echo "$e" | grep -q ":/var/lib/nimbus-control:/usr/sbin/nologin$"'
for g in docker libvirt nimbus-smb; do check "nimbus-control is in group $g" in_group nimbus-control "$g"; done
check "group nimbus-control exists (for desktop users)" getent group nimbus-control
check "user nimbus-guest: primary group nimbus-smb, nologin" \
	sh -c '[ "$(id -gn nimbus-guest)" = nimbus-smb ] && getent passwd nimbus-guest | grep -q ":/usr/sbin/nologin$"'
check "$STATE 0750 nimbus-control" mode_owner "$STATE" "750 nimbus-control:nimbus-control"
check "$STATE/stacks 0750 nimbus-control" mode_owner "$STATE/stacks" "750 nimbus-control:nimbus-control"
check "/var/lib/nimbus-vms/isos 2775 root:nimbus-smb" mode_owner /var/lib/nimbus-vms/isos "2775 root:nimbus-smb"
check "/var/lib/nimbus-vms/images 0711 root" mode_owner /var/lib/nimbus-vms/images "711 root:root"
check "/var/lib/nimbus-privd 0700 root" mode_owner /var/lib/nimbus-privd "700 root:root"
check "/run/nimbus-control/vnc-tokens 0750 nimbus-control" mode_owner /run/nimbus-control/vnc-tokens "750 nimbus-control:nimbus-control"
check "generated Samba files exist (empty)" test -f /etc/samba/nimbus/settings.conf -a -f /etc/samba/nimbus-shares.conf
check "smb.conf has the include exactly once" test "$(grep -cxF 'include = /etc/samba/nimbus/nimbus.conf' /etc/samba/smb.conf)" = 1
check "testparm accepts the configuration" testparm -s
check "Samba: guest account = nimbus-guest" test "$(smb_param 'guest account')" = nimbus-guest
check "Samba: multicast dns register = No" test "$(smb_param 'multicast dns register')" = No
check "Samba: disable netbios = Yes" test "$(smb_param 'disable netbios')" = Yes
# testparm prints a protocol under its canonical name ("SMB2" for SMB2_10), so
# compare with what it prints for an explicit SMB2_10.
printf '[global]\n   server min protocol = SMB2_10\n' >"$WORK/smb2_10.conf"
smb2_10="$(testparm -s --parameter-name='server min protocol' "$WORK/smb2_10.conf" 2>/dev/null)"
check "Samba: server min protocol is SMB2_10 (testparm: '$(smb_param 'server min protocol')')" \
	test "$(smb_param 'server min protocol')" = "$smb2_10"
check "adduser.conf: EXTRA_GROUPS and ADD_EXTRA_GROUPS set" \
	sh -c "grep -qx 'EXTRA_GROUPS=\"$NIMBUS_USER_EXTRA_GROUPS\"' /etc/adduser.conf && grep -qx 'ADD_EXTRA_GROUPS=1' /etc/adduser.conf"
check "nmbd disabled, smbd still enabled" sh -c '! systemctl is-enabled nmbd.service | grep -qx enabled && systemctl is-enabled smbd.service | grep -qx enabled'
check "libvirt default network marked for autostart" test -L /etc/libvirt/qemu/networks/autostart/default.xml
for u in nimbus-control.service nimbus-privd.socket nimbus-snapraid-sync.timer nimbus-recycle-clean.timer; do
	check "$u enabled" is_enabled "$u"
done
check "nimbus-netfix runs whenever docker.service starts" test -L /etc/systemd/system/docker.service.wants/nimbus-netfix.service
check "units started only through deb-systemd-invoke" called '^postinst deb-systemd-invoke start .*nimbus-control\.service'
check "policy-rc.d was consulted (and refused, as in the image build)" grep -q 'nimbus-control.service' /tmp/policy-rc.d.log
check "systemctl used only to check and disable nmbd" \
	sh -c '! grep " systemctl " '"$LOG"' | grep -Ev "systemctl --quiet (is-enabled|--no-reload disable) nmbd\.service$"'
check "no docker, virsh, smbcontrol, invoke-rc.d or service call" sh -c '! grep -Eq " (docker|virsh|smbcontrol|invoke-rc\.d|service) " '"$LOG"
read -ra exec_start_pre <<<"$(sed -n 's/^ExecStartPre=//p' /usr/lib/systemd/system/nimbus-control.service)"   # the command and its arguments
check "ExecStartPre (nimbus-migrate as nimbus-control) creates schema 1" \
	sh -c '"$@" && [ "$(cat '"$STATE"'/schema_version)" = 1 ] && [ "$(stat -c %U '"$STATE"'/stacks.json)" = nimbus-control ]' _ "${SVC_RUN[@]}" "${exec_start_pre[@]}"

# --- user data and administrator changes, as a system in use would have them ---------
mkdir -p "$STATE/stacks/demo/data" /mnt/user/Media /mnt/disks/disk1 /mnt/parity/parity1 /srv/nimbus/shares/Docs \
	/var/lib/nimbus-privd/fstab-backups
printf 'services:\n  grafana:\n    image: grafana/grafana\n' >"$STATE/stacks/demo/docker-compose.yml"
head -c 65536 /dev/urandom >"$STATE/stacks/demo/data/grafana.db"
chown -R 472:472 "$STATE/stacks/demo/data"
chown nimbus-control: "$STATE/stacks/demo" "$STATE/stacks/demo/docker-compose.yml"
head -c 1048576 /dev/urandom >/var/lib/nimbus-vms/images/vm1.qcow2
head -c 262144 /dev/urandom >/var/lib/nimbus-vms/isos/debian.iso
head -c 131072 /dev/urandom >/mnt/user/Media/film.mkv
head -c 4096 /dev/urandom >/mnt/disks/disk1/notes.txt
head -c 4096 /dev/urandom >/mnt/parity/parity1/snapraid.parity
echo "minutes of the meeting" >/srv/nimbus/shares/Docs/minutes.txt
printf 'parity /mnt/parity/parity1/snapraid.parity\ndata d1 /mnt/disks/disk1\n' >/etc/nimbus/snapraid.conf
echo '{"alice": {"created": "2026-11-17"}}' >/var/lib/nimbus-privd/smb-users.json
cp /etc/fstab /var/lib/nimbus-privd/fstab-backups/fstab.20261117T120000Z
printf '# BEGIN nimbus\nUUID=0b9a5a0e-3d1c-4b8e-9b55-1f0f7d1c2a11 /mnt/disks/disk1 xfs defaults,nofail 0 2\n# END nimbus\n' >>/etc/fstab
adduser --quiet --system --no-create-home --home /nonexistent --shell /usr/sbin/nologin --ingroup nimbus-smb alice
printf 'Tr0ub4dor&3\nTr0ub4dor&3\n' | smbpasswd -a -s alice >/dev/null
printf '[Media]\n   path = /mnt/user/Media\n   read only = no\n   force group = nimbus-smb\n' >/etc/samba/nimbus-shares.conf
printf 'workgroup = NIMBUS\n' >/etc/samba/nimbus/settings.conf
printf '<?xml version="1.0" standalone="no"?>\n<!-- Generated by nimbus-control -->\n<service-group><name>%%h</name></service-group>\n' \
	>/etc/avahi/services/smb.service
echo '{"theme": "dark"}' >"$STATE/settings.json"
chown nimbus-control: "$STATE/settings.json"
systemctl --quiet enable nmbd.service            # administrator turns NetBIOS back on
systemctl --quiet disable wsdd2.service          # ... and WS-Discovery off
sed -i -E "s/^EXTRA_GROUPS=.*/EXTRA_GROUPS=\"$NIMBUS_USER_EXTRA_GROUPS audio\"/" /etc/adduser.conf
check "testparm accepts the configuration with a share and settings" testparm -s
check "SMB user alice has a Samba password" sh -c 'pdbedit -L | grep -q "^alice:"'
data_before="$(digest)"
state_before="$(cd "$STATE" && sha256sum schema_version settings.json shares.json stacks.json)"

# --- 3. upgrade ----------------------------------------------------------------------
echo "Upgrade to $(dpkg-deb -f "$deb" Version)"
: >"$LOG"
dpkg -i "$deb" >/tmp/upgrade.log 2>&1 || { cat /tmp/upgrade.log; exit 1; }
check "include still exactly once" test "$(grep -cxF 'include = /etc/samba/nimbus/nimbus.conf' /etc/samba/smb.conf)" = 1
check "administrator's nmbd (on) and wsdd2 (off) are kept" sh -c 'systemctl is-enabled nmbd.service | grep -qx enabled && ! systemctl is-enabled wsdd2.service | grep -qx enabled'
check "administrator's EXTRA_GROUPS is kept" grep -qx "EXTRA_GROUPS=\"$NIMBUS_USER_EXTRA_GROUPS audio\"" /etc/adduser.conf
check "files changed, so nimbus-control is restarted" called '^postinst deb-systemd-invoke restart nimbus-control\.service$'
check "  ... and privd stopped (socket activation brings the new one)" called '^postinst deb-systemd-invoke stop nimbus-privd\.service$'
check "nothing stopped before the upgrade" sh -c '! grep -q "^prerm deb-systemd-invoke stop" '"$LOG"
check "user data unchanged" test "$(digest)" = "$data_before"
check "state files unchanged" test "$(cd "$STATE" && sha256sum schema_version settings.json shares.json stacks.json)" = "$state_before"
check "ExecStartPre at the same schema changes nothing" \
	sh -c '"$@" && [ "$(cat '"$STATE"'/schema_version)" = 1 ]' _ "${SVC_RUN[@]}" "${exec_start_pre[@]}"
: >"$LOG"
dpkg -i "$deb" >/tmp/reinstall.log 2>&1 || { cat /tmp/reinstall.log; exit 1; }
check "reinstall with unchanged files: no restart, only start" \
	sh -c '! grep -q restart '"$LOG"' && grep -q "^postinst deb-systemd-invoke start nimbus-control.service$" '"$LOG"
check "no docker, virsh, smbcontrol, invoke-rc.d or service call" sh -c '! grep -Eq " (docker|virsh|smbcontrol|invoke-rc\.d|service) " '"$LOG"

echo "Migrations with the installed nimbus-migrate, as nimbus-control"
steps="$WORK/steps"
mkdir -p "$steps"
cp /usr/lib/nimbus-control/migrations/*.py "$steps/"
cat >"$steps/0002_share_ids.py" <<'EOF'
DESCRIPTION = "number the shares"
def migrate(ctx):
    data = ctx.read_json("shares.json")
    data["numbered"] = True
    ctx.write_json("shares.json", data)
EOF
chmod -R a+rX "$WORK"
migrate=("${SVC_RUN[@]}" /usr/lib/nimbus-control/bin/nimbus-migrate --migrations-dir "$steps")
stacks_before="$(cd "$STATE/stacks" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)"
check "step 2 runs as the service user, with a backup it owns" \
	sh -c '"$@" 2>/dev/null && [ "$(cat '"$STATE"'/schema_version)" = 2 ] && b=$(ls -d '"$STATE"'/backups/schema-1-*) && [ "$(stat -c %U "$b/settings.json")" = nimbus-control ]' _ "${migrate[@]}"
printf 'DESCRIPTION = "break things"\ndef migrate(ctx):\n    ctx.write_json("settings.json", {})\n    raise OSError(28, "No space left on device")\n' >"$steps/0003_break.py"
chmod a+r "$steps/0003_break.py"
settings_before="$(sha256sum <"$STATE/settings.json")"
out="$("${migrate[@]}" 2>&1)" && rc=0 || rc=$?
check "a failing step 3 exits 1" test "$rc" = 1
check "  ... restores the state (schema 2, settings.json unchanged)" \
	sh -c '[ "$(cat '"$STATE"'/schema_version)" = 2 ] && [ "$(sha256sum <'"$STATE"'/settings.json)" = "$1" ]' _ "$settings_before"
check "  ... with one clear message naming the step" \
	sh -c 'printf "%s\n" "$1" | grep -c "state migration 0003 (break things) failed: .*No space left on device.*restored from.*will not start" | grep -qx 1' _ "$out"
check "the packaged steps refuse state from a newer version (schema 2)" \
	sh -c '"$@" 2>/dev/null; [ $? -eq 3 ]' _ "${SVC_RUN[@]}" "${exec_start_pre[@]}"
check "stacks/ untouched by all of this" \
	test "$(cd "$STATE/stacks" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)" = "$stacks_before"

# --- 4. remove and purge ---------------------------------------------------------------
echo "Remove"
: >"$LOG"
dpkg --remove nimbus-control >/tmp/remove.log 2>&1 || { cat /tmp/remove.log; exit 1; }
check "remove stops the units through deb-systemd-invoke" called '^prerm deb-systemd-invoke stop .*nimbus-control\.service'
check "remove keeps the state, the include and the Samba files" \
	test -f "$STATE/settings.json" -a -f /etc/samba/nimbus/nimbus.conf -a -f /etc/samba/nimbus-shares.conf
check "remove keeps all user data" test "$(digest)" = "$data_before"

echo "Purge"
dpkg --purge nimbus-control >/tmp/purge.log 2>&1 || { cat /tmp/purge.log; exit 1; }
check "all user data is still there, byte for byte (and owners)" test "$(digest)" = "$data_before"
for p in "$STATE/stacks" /var/lib/nimbus-vms /mnt/user /mnt/disks /mnt/parity /srv/nimbus/shares \
	/etc/nimbus/snapraid.conf /var/lib/nimbus-privd /etc/fstab "SMB users" "system accounts"; do
	check "purge message names: $p" grep -q -- "  $p " /tmp/purge.log
done
check "purge message names SMB user alice" grep -q 'SMB users .*alice' /tmp/purge.log
check "SMB user alice and her Samba password remain" sh -c 'getent passwd alice && pdbedit -L | grep -q "^alice:"'
check "accounts and groups remain" sh -c 'getent passwd nimbus-control && getent passwd nimbus-guest && getent group nimbus-smb'
check "the Control Center's state files and backups are gone" \
	sh -c '[ -z "$(find '"$STATE"' -maxdepth 1 -type f)" ] && [ ! -e '"$STATE"'/backups ]'
check "privd's cache and the package bookkeeping are gone" test ! -e /var/cache/nimbus-privd -a ! -e /var/lib/nimbus-privd/package
check "generated Samba files, nimbus.conf and the Avahi service are gone" \
	test ! -e /etc/samba/nimbus -a ! -e /etc/samba/nimbus-shares.conf -a ! -e /etc/avahi/services/smb.service
check "smb.conf is byte-identical to before the install" cmp -s /etc/samba/smb.conf "$WORK/smb.conf.before"
check "testparm still accepts smb.conf" testparm -s
check "adduser.conf: ADD_EXTRA_GROUPS restored, the administrator's EXTRA_GROUPS kept" \
	sh -c '! grep -q "^ADD_EXTRA_GROUPS=" /etc/adduser.conf && grep -qx "EXTRA_GROUPS=\"'"$NIMBUS_USER_EXTRA_GROUPS"' audio\"" /etc/adduser.conf'
check "no docker, virsh, smbcontrol, invoke-rc.d or service call" sh -c '! grep -Eq " (docker|virsh|smbcontrol|invoke-rc\.d|service) " '"$LOG"
echo
echo "--- purge output ---"
sed -n '/nimbus-control: removed/,$p' /tmp/purge.log
echo "--------------------"

# Once more from Debian's own adduser.conf: install, purge, byte-identical.
cp -p "$WORK/adduser.conf.before" /etc/adduser.conf
dpkg -i "$deb" >/dev/null 2>&1 && dpkg --purge nimbus-control >/dev/null 2>&1
check "fresh install + purge: adduser.conf byte-identical" cmp -s /etc/adduser.conf "$WORK/adduser.conf.before"
check "fresh install + purge: smb.conf byte-identical" cmp -s /etc/samba/smb.conf "$WORK/smb.conf.before"

echo
printf 'nimbus-control maintscripts.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
