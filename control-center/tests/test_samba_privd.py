# =============================================================================
# control-center/tests/test_samba_privd.py: shares, SMB users, Samba settings
# =============================================================================
# What: samba.py and nimbus_common.shares with every command replaced and the
#       Samba, Avahi and storage paths in a temporary directory:
#         - share checks: names, security, users and access, hosts, recycle
#           days, Time Machine size, duplicate names ignoring case;
#         - rendering: guest mapping only with Public or Secure shares, each
#           security mode's lines, recycle bin and Time Machine, hosts allowed,
#           a share nobody may use or whose disk is gone published as
#           unavailable, the Avahi file with _adisk only for Time Machine;
#         - share.apply: testparm on a candidate set first (its stderr read,
#           because it exits 0 on unknown parameters), files swapped,
#           smbd reloaded, the live configuration checked, the old files
#           restored on failure; share folders root:<group> 2775, never
#           through a link; the include put back into smb.conf; the isos
#           share bound onto the ISO library with its ISOs moved in;
#         - share.delete_data: refused while exported, link-safe;
#         - SMB users: system accounts for new users, desktop accounts only
#           join the group, system accounts refused, rollback when smbpasswd
#           fails, passwords only ever on stdin or in a 0600 file;
#         - samba.settings: the systemctl calls for SMB and NetBIOS.
# Why:  spec Tab C (Shares, SMB users, Samba config generation); NOTE-68.
# =============================================================================

import grp
import os
import pwd
import stat
import subprocess

import pytest

from nimbus_common import shares as S
from nimbus_common.validate import ValidationError
from nimbus_privd import fsutil, runner, samba

GROUP = grp.getgrgid(os.getgid()).gr_name
PASSWORD = "c0rrect-H0rse"


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.answers = []                 # (prefix, (rc, stdout, stderr) or callable)

    def run(self, argv, *, input=None, timeout=60, check=True, env=None):
        self.calls.append((list(argv), input))
        for prefix, answer in self.answers:
            if tuple(argv[:len(prefix)]) == prefix:
                rc, out, err = answer(argv, input) if callable(answer) else answer
                if check and rc:
                    raise runner.CommandFailed(argv, rc, out + err)
                return subprocess.CompletedProcess(argv, rc, out, err)
        raise AssertionError(f"unexpected command {argv}")

    def argvs(self, program):
        return [a for a, _ in self.calls if a[0] == program]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("DISKS", "mnt/disks"), ("PARITY", "mnt/parity"), ("POOL", "mnt/user"),
                      ("SYSTEM_SHARES", "srv/shares"), ("ISOS", "isos")):
        (tmp_path / sub).mkdir(parents=True)
        monkeypatch.setattr(S, name, str(tmp_path / sub))
    etc = tmp_path / "etc"
    (etc / "samba/nimbus").mkdir(parents=True)
    paths = {"SMB_CONF": etc / "samba/smb.conf", "NIMBUS_CONF": etc / "samba/nimbus/nimbus.conf",
             "SETTINGS_CONF": etc / "samba/nimbus/settings.conf", "SHARES_CONF": etc / "samba/nimbus-shares.conf",
             "AVAHI": etc / "avahi/services/smb.service", "REGISTRY_FILE": tmp_path / "privd/smbusers.json"}
    for name, path in paths.items():
        monkeypatch.setattr(samba, name, str(path))
    monkeypatch.setattr(samba, "GROUP", GROUP)
    monkeypatch.setattr(samba, "INCLUDE_LINE", f"include = {paths['NIMBUS_CONF']}")
    monkeypatch.setattr(fsutil, "FSTAB", str(etc / "fstab"))
    monkeypatch.setattr(fsutil, "BACKUPS", str(tmp_path / "fstab-backups"))
    paths["SMB_CONF"].write_text(f"[global]\n   workgroup = DEBIAN\n\n# BEGIN nimbus-control\ninclude = {paths['NIMBUS_CONF']}\n"
                                 "# END nimbus-control\n")
    paths["NIMBUS_CONF"].write_text(f"[global]\n   server min protocol = SMB2_10\n   include = {paths['SETTINGS_CONF']}\n"
                                    f"   include = {paths['SHARES_CONF']}\n")
    mounted = set()
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: p in mounted or real(p))
    fake = FakeRunner()
    fake.seen = []

    def testparm(argv, _):
        conf = argv[-1]
        with open(conf, encoding="utf-8") as f:
            text = f.read()
        fake.seen.append((conf, text))
        return 0, "", "Loaded services file OK.\n"
    monkeypatch.setattr(samba, "smbd_running", lambda: True)
    fake.answers += [((samba.TESTPARM,), testparm), ((samba.SMBCONTROL,), (0, "", "")), ((samba.MOUNT,), lambda a, i: mounted.add(a[1]) or (0, "", "")),
                     ((samba.UMOUNT,), lambda a, i: mounted.discard(a[1]) or (0, "", ""))]
    monkeypatch.setattr(runner, "run", fake.run)
    return tmp_path, paths, mounted, fake


def share(name="media", **changes):
    spec = {"name": name, "location": "system", "security": "secure", "users": {"alice": "rw"}}
    spec.update(changes)
    return S.share_spec(spec, "")


SETTINGS = {"enabled": True, "workgroup": "WORKGROUP", "description": "NimbusOS", "netbios": False}


# --- checks -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("changes, field", [
    ({"name": "../etc"}, "name"), ({"name": "global"}, "name"), ({"security": "open"}, "security"),
    ({"users": {"Alice": "rw"}}, "users"), ({"users": {"alice": "admin"}}, "users"),
    ({"hosts_allow": "192.168.1.0/24; rm"}, "hosts_allow"), ({"recycle": {"enabled": True, "days": 0}}, "recycle.days"),
    ({"timemachine": {"enabled": True, "max_gb": -1}}, "timemachine.max_gb"), ({"location": "disk:../x"}, "location"),
    ({"location": "/etc"}, "location"), ({"smb": "yes"}, "smb"), ({"notes": "x" * 2001}, "notes"),
])
def test_share_checks(changes, field):
    with pytest.raises(ValidationError) as err:
        S.share_spec({"name": "media", "location": "system", "security": "private", **changes}, "")
    assert err.value.field == field


def test_share_defaults_and_lists():
    s = S.share_spec({"name": "Media", "users": {"bob": "none", "alice": "ro"}}, "")
    assert s == {"name": "Media", "location": "pool", "smb": True, "security": "private", "users": {"alice": "ro"},
                 "browseable": True, "recycle": {"enabled": False, "days": 30}, "timemachine": {"enabled": False, "max_gb": 0},
                 "hosts_allow": "", "notes": ""}
    with pytest.raises(ValidationError) as err:
        S.share_list([{"name": "Media"}, {"name": "media"}])
    assert "same name" in err.value.message
    assert S.share_spec({"name": "a", "hosts_allow": "192.168.1.0/24, nas.local  fe80::1"}, "")["hosts_allow"] == \
        "192.168.1.0/24 nas.local fe80::1"
    with pytest.raises(ValidationError):
        S.samba_settings({"workgroup": "TOO-LONG-WORKGROUP"})
    with pytest.raises(ValidationError):
        S.samba_settings({"description": "x\ninclude = /etc/passwd"})


# --- rendering ----------------------------------------------------------------------------------------
def test_render_settings():
    text = samba.render_settings(SETTINGS, [share(security="private")])
    assert "workgroup = WORKGROUP" in text and "server string = NimbusOS" in text and "disable netbios = yes" in text
    assert "map to guest = Never" in text
    assert "map to guest = Bad User" in samba.render_settings(SETTINGS, [share(security="public")])
    assert "map to guest = Never" in samba.render_settings(SETTINGS, [share(security="public", smb=False)])
    assert "disable netbios = no" in samba.render_settings({**SETTINGS, "netbios": True}, [])


def lines(text):
    return [line.strip() for line in text.splitlines()]


def test_render_security_modes(env):
    public = lines(samba.render_share(share(security="public", users={}), True))
    assert "guest ok = yes" in public and "read only = no" in public and not any(x.startswith("valid users") for x in public)
    secure = lines(samba.render_share(share(users={"alice": "rw", "bob": "ro"}), True))
    assert "guest ok = yes" in secure and "read only = yes" in secure and "write list = alice" in secure
    private = lines(samba.render_share(share(security="private", users={"alice": "rw", "bob": "ro", "carol": "none"}), True))
    assert "guest ok = no" in private and "valid users = alice bob" in private and "write list = alice" in private
    assert "available = no" not in private
    nobody = lines(samba.render_share(share(security="private", users={}), True))
    assert "available = no" in nobody
    common = [f"force group = {GROUP}", "create mask = 0664", "force create mode = 0664", "directory mask = 2775",
              "force directory mode = 2775", "vfs objects = catia fruit streams_xattr", f"path = {S.SYSTEM_SHARES}/media"]
    assert all(line in secure for line in common)


def test_render_options(env):
    text = lines(samba.render_share(share(browseable=False, recycle={"enabled": True, "days": 7},
                                          timemachine={"enabled": True, "max_gb": 500}, hosts_allow="192.168.1.0/24"), False))
    assert "browseable = no" in text and "vfs objects = catia fruit streams_xattr recycle" in text
    assert "recycle:repository = .recycle" in text and "recycle:touch_mtime = yes" in text
    assert "fruit:time machine = yes" in text and "fruit:time machine max size = 500G" in text
    assert "hosts allow = 192.168.1.0/24 127.0.0.1" in text and "available = no" in text


def test_render_avahi():
    plain = samba.render_avahi([share()])
    assert "Generated by nimbus-control" in plain and "<type>_smb._tcp</type><port>445</port>" in plain
    assert "_adisk" not in plain
    tm = samba.render_avahi([share(), share("timemachine", timemachine={"enabled": True, "max_gb": 0})])
    assert "<type>_adisk._tcp</type>" in tm and "dk0=adVN=timemachine,adVF=0x82" in tm


# --- share.apply ----------------------------------------------------------------------------------------
def test_apply(env):
    tmp, paths, mounted, fake = env
    shares = [share(), share("backups", security="private", recycle={"enabled": True, "days": 30})]
    result = samba.share_apply(Ctx(), {"shares": shares, "settings": SETTINGS})
    assert result == {"shares": 2, "exported": 2, "unavailable": []}
    candidate, live = fake.seen
    assert candidate[0] != str(paths["SMB_CONF"]) and live[0] == str(paths["SMB_CONF"])
    assert "[media]" in paths["SHARES_CONF"].read_text() and "[backups]" in paths["SHARES_CONF"].read_text()
    assert "map to guest = Bad User" in paths["SETTINGS_CONF"].read_text()
    assert stat.S_IMODE(paths["SHARES_CONF"].stat().st_mode) == 0o644
    assert [a for a, _ in fake.calls if a[0] == samba.SMBCONTROL] == [[samba.SMBCONTROL, "smbd", "reload-config"]]
    for name in ("media", "backups"):
        st = os.stat(f"{S.SYSTEM_SHARES}/{name}")
        assert stat.S_IMODE(st.st_mode) == 0o2775 and st.st_gid == grp.getgrnam(GROUP).gr_gid
    assert "<type>_smb._tcp</type>" in paths["AVAHI"].read_text()


def test_the_candidate_is_checked_before_anything_changes(env):
    tmp, paths, mounted, fake = env
    samba.share_apply(Ctx(), {"shares": [share()], "settings": SETTINGS})
    before = {k: paths[k].read_text() for k in ("SHARES_CONF", "SETTINGS_CONF")}
    fake.answers.insert(0, ((samba.TESTPARM,), (0, "", "Unknown parameter encountered: \"fruit:bogus\"\n"
                                                        "Ignoring unknown parameter \"fruit:bogus\"\nLoaded services file OK.\n")))
    with pytest.raises(runner.CommandFailed) as err:
        samba.share_apply(Ctx(), {"shares": [share(), share("docs")], "settings": SETTINGS})
    assert "Unknown parameter" in str(err.value)
    assert {k: paths[k].read_text() for k in before} == before


def test_a_failing_live_check_restores_the_old_files(env):
    tmp, paths, mounted, fake = env
    samba.share_apply(Ctx(), {"shares": [share()], "settings": SETTINGS})
    before = paths["SHARES_CONF"].read_text()
    calls = {"n": 0}

    def flaky(argv, _):
        calls["n"] += 1
        return (0, "", "") if calls["n"] == 1 else (1, "", "Error loading services.\n")
    fake.answers.insert(0, ((samba.TESTPARM,), flaky))
    ctx = Ctx()
    with pytest.raises(runner.CommandFailed):
        samba.share_apply(ctx, {"shares": [share(), share("docs")], "settings": SETTINGS})
    assert paths["SHARES_CONF"].read_text() == before and "[docs]" not in before
    assert "The previous Samba configuration was restored." in ctx.lines


def test_the_include_is_put_back(env):
    tmp, paths, mounted, fake = env
    paths["SMB_CONF"].write_text("[global]\n   workgroup = DEBIAN\n")
    ctx = Ctx()
    samba.share_apply(ctx, {"shares": [], "settings": SETTINGS})
    assert samba.INCLUDE_LINE in paths["SMB_CONF"].read_text() and any("was missing" in line for line in ctx.lines)
    assert not paths["AVAHI"].exists()


def test_a_missing_disk_publishes_the_share_unavailable(env):
    tmp, paths, mounted, fake = env
    result = samba.share_apply(Ctx(), {"shares": [share("films", location="disk:disk2")], "settings": SETTINGS})
    assert result["unavailable"] == ["films"] and "available = no" in paths["SHARES_CONF"].read_text()
    assert not os.path.exists(f"{S.DISKS}/disk2/films")                    # nothing made on the system disk


def test_share_folders_never_follow_links(env):
    tmp, paths, mounted, fake = env
    (tmp / "elsewhere").mkdir()
    os.symlink(tmp / "elsewhere", f"{S.SYSTEM_SHARES}/media")
    with pytest.raises(ValidationError) as err:
        samba.share_apply(Ctx(), {"shares": [share()], "settings": SETTINGS})
    assert "not a plain folder" in err.value.message
    assert stat.S_IMODE(os.stat(tmp / "elsewhere").st_mode) != 0o2775


def test_the_isos_share_becomes_the_iso_library(env):
    tmp, paths, mounted, fake = env
    (tmp / "isos/debian.iso").write_bytes(b"iso")
    mounted.add(S.POOL)
    samba.share_apply(Ctx(), {"shares": [share("isos", location="pool")], "settings": SETTINGS})
    assert (tmp / "mnt/user/isos/debian.iso").read_bytes() == b"iso" and not (tmp / "isos/debian.iso").exists()
    assert fsutil.read_block() == [{"kind": "isos", "source": f"{S.POOL}/isos"}]
    assert [samba.MOUNT, S.ISOS] in [a for a, _ in fake.calls]
    samba.share_apply(Ctx(), {"shares": [], "settings": SETTINGS})
    assert fsutil.read_block() == [] and [samba.UMOUNT, S.ISOS] in [a for a, _ in fake.calls]


def test_delete_data(env):
    tmp, paths, mounted, fake = env
    samba.share_apply(Ctx(), {"shares": [share()], "settings": SETTINGS})
    with pytest.raises(ValidationError) as err:
        samba.share_delete_data(Ctx(), {"name": "media", "location": "system"})
    assert "still shared" in err.value.message
    samba.share_apply(Ctx(), {"shares": [], "settings": SETTINGS})
    (tmp / "keep").mkdir()
    (tmp / "keep/precious").write_text("keep")
    os.symlink(tmp / "keep", f"{S.SYSTEM_SHARES}/media/link")
    (tmp / "srv/shares/media/file").write_text("x")
    assert samba.share_delete_data(Ctx(), {"name": "media", "location": "system"}) == {"deleted": f"{S.SYSTEM_SHARES}/media"}
    assert not os.path.exists(f"{S.SYSTEM_SHARES}/media") and (tmp / "keep/precious").read_text() == "keep"
    with pytest.raises(ValidationError):
        samba.share_delete_data(Ctx(), {"name": "x", "location": "disk:disk9"})


# --- SMB users ----------------------------------------------------------------------------------------------
@pytest.fixture
def users(env, monkeypatch):
    tmp, paths, mounted, fake = env
    known = {"alice": 1000, "postgres": 110}
    in_samba = set()
    monkeypatch.setattr(samba, "_account", lambda n: pwd.struct_passwd((n, "x", known[n], known[n], "", "/", "/bin/sh"))
                        if n in known else None)

    def adduser(argv, _):
        known[argv[-1]] = 120
        return 0, "", ""

    def smbpasswd(argv, stdin):
        if "-x" in argv:
            in_samba.discard(argv[-1])
        elif "-a" in argv:
            in_samba.add(argv[-1])
        return 0, "", ""
    fake.answers += [((samba.PDBEDIT,), lambda a, i: (0, "".join(f"{u}:{known.get(u, 0)}:\n" for u in sorted(in_samba)), "")),
                     ((samba.ADDUSER,), adduser), ((samba.SMBPASSWD,), smbpasswd), ((samba.GPASSWD,), (0, "", "")),
                     ((samba.DELUSER,), (0, "", ""))]
    fake.in_samba = in_samba
    return fake


def test_add_an_smb_only_user(users):
    samba.smbuser_add(Ctx(), {"user": "bob", "password": PASSWORD})
    assert users.argvs(samba.ADDUSER) == [[samba.ADDUSER, "--system", "--no-create-home", "--home", "/nonexistent",
                                           "--shell", "/usr/sbin/nologin", "--ingroup", GROUP, "--comment",
                                           "NimbusOS SMB user", "bob"]]
    assert ([samba.SMBPASSWD, "-a", "-s", "bob"], f"{PASSWORD}\n{PASSWORD}\n") in users.calls
    listed = samba.smbuser_list(Ctx(), {})["users"]
    assert listed == [{"name": "bob", "uid": 120, "managed": True, "desktop": False}]
    with pytest.raises(ValidationError) as err:
        samba.smbuser_add(Ctx(), {"user": "bob", "password": PASSWORD})
    assert "already an SMB user" in err.value.message


def test_desktop_and_system_accounts(users):
    samba.smbuser_add(Ctx(), {"user": "alice", "password": PASSWORD})
    assert users.argvs(samba.GPASSWD) == [[samba.GPASSWD, "-a", "alice", GROUP]] and not users.argvs(samba.ADDUSER)
    assert samba.smbuser_list(Ctx(), {})["users"][0]["desktop"] is True
    with pytest.raises(ValidationError) as err:
        samba.smbuser_add(Ctx(), {"user": "postgres", "password": PASSWORD})
    assert "system account" in err.value.message


def test_a_failed_password_rolls_the_account_back(users):
    users.answers.insert(0, ((samba.SMBPASSWD,), (1, "", "Failed to add entry\n")))
    with pytest.raises(runner.CommandFailed):
        samba.smbuser_add(Ctx(), {"user": "carol", "password": PASSWORD})
    assert [samba.DELUSER, "--system", "carol"] in users.argvs(samba.DELUSER)
    assert "carol" not in samba._registry()


def test_password_and_delete(users):
    samba.smbuser_add(Ctx(), {"user": "bob", "password": PASSWORD})
    samba.smbuser_add(Ctx(), {"user": "alice", "password": PASSWORD})
    samba.smbuser_passwd(Ctx(), {"user": "bob", "password": "N3w-Passw0rd"})
    assert ([samba.SMBPASSWD, "-s", "bob"], "N3w-Passw0rd\nN3w-Passw0rd\n") in users.calls
    samba.smbuser_delete(Ctx(), {"user": "bob"})
    samba.smbuser_delete(Ctx(), {"user": "alice"})
    assert [samba.DELUSER, "--system", "bob"] in users.argvs(samba.DELUSER)
    assert [samba.GPASSWD, "-d", "alice", GROUP] in users.argvs(samba.GPASSWD)
    with pytest.raises(ValidationError):
        samba.smbuser_passwd(Ctx(), {"user": "bob", "password": PASSWORD})
    assert not any(PASSWORD in " ".join(a) or "N3w-Passw0rd" in " ".join(a) for a, _ in users.calls)   # never in argv


def test_connection_test(env):
    tmp, paths, mounted, fake = env
    seen = {}

    def smbclient(argv, _):
        path = argv[argv.index("-A") + 1]
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        seen["text"] = open(path).read()
        seen["path"] = path
        return 0, "Disk|media|\nDisk|backups|\nIPC|IPC$|IPC Service\n", ""
    fake.answers.append(((samba.SMBCLIENT,), smbclient))
    result = samba.samba_test(Ctx(), {"user": "bob", "password": PASSWORD})
    assert result == {"ok": True, "shares": ["media", "backups"], "message": "Connected as bob: 2 share(s) visible."}
    assert seen["mode"] == 0o600 and seen["text"] == f"username = bob\npassword = {PASSWORD}\n"
    assert not os.path.exists(seen["path"]) and PASSWORD not in " ".join(fake.calls[-1][0])
    fake.answers.insert(0, ((samba.SMBCLIENT,), (1, "", "session setup failed: NT_STATUS_LOGON_FAILURE\n")))
    assert samba.samba_test(Ctx(), {"user": "bob", "password": PASSWORD})["message"] == \
        "Connecting failed: wrong user name or password."


def test_samba_settings(env, monkeypatch):
    tmp, paths, mounted, fake = env
    assert samba.samba_settings(Ctx(), {"enabled": True, "netbios": False})["applied"] is False   # no systemd here
    real = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda p: True if p == "/run/systemd/system" else real(p))
    fake.answers.append(((samba.SYSTEMCTL,), (0, "", "")))
    samba.samba_settings(Ctx(), {"enabled": True, "netbios": True})
    samba.samba_settings(Ctx(), {"enabled": True, "netbios": False})
    samba.samba_settings(Ctx(), {"enabled": False, "netbios": False})
    assert fake.argvs(samba.SYSTEMCTL) == [
        [samba.SYSTEMCTL, "enable", "--now", "smbd.service", "wsdd2.service", "nmbd.service"],
        [samba.SYSTEMCTL, "enable", "--now", "smbd.service", "wsdd2.service"],
        [samba.SYSTEMCTL, "disable", "--now", "nmbd.service"],
        [samba.SYSTEMCTL, "disable", "--now", "smbd.service", "wsdd2.service", "nmbd.service"]]
