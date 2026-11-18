# =============================================================================
# control-center/tests/test_nas_api.py: the Storage, Shares and Settings API
# =============================================================================
# What: the real app with a stateful fake of privd's storage and Samba verbs
#       (FakeNas: disks, fstab entries, SMB users, the last applied shares):
#         - the storage overview: pool mode from fstab and parity, missing
#           disks, shares per location, the live session;
#         - disk jobs (format, add, unmount) with their refusals; pool changes
#           (refused when they would strand shares; shares applied again);
#         - the wizard: every check (serials, modes, shares, user) and the
#           exact privd calls for Pooled + parity with two new disks;
#         - shares: create, edit, delete (with and without files) as jobs,
#           saved only when share.apply worked; field errors; the list with
#           folders, free space and connection strings; the Apps tab sees them;
#         - SMB users and Test connection; Settings with their checks, and
#           Samba changes applied through privd.
# Why:  spec Tab C and Settings; docs/PLAN.md NOTE-68.
# =============================================================================

import json
import os
import socket
import types

import pytest
from fastapi.testclient import TestClient

from conftest import CC, FakeLibvirt, FakeNas, FakePrivd
from nimbus_common import shares as S
from nimbus_control.app import create_app
from nimbus_control.config import Settings

ORIGIN = "http://127.0.0.1:7777"
WS = "ws://127.0.0.1:7777"


@pytest.fixture
def w(tmp_path, monkeypatch):
    for name, sub in (("DISKS", "mnt/disks"), ("PARITY", "mnt/parity"), ("POOL", "mnt/user"), ("SYSTEM_SHARES", "srv/shares")):
        (tmp_path / sub).mkdir(parents=True)
        monkeypatch.setattr(S, name, str(tmp_path / sub))
    mounted = {str(tmp_path / "mnt/disks/disk1")}
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: p in mounted or real(p))
    (tmp_path / "state").mkdir()
    nas = FakeNas()
    privd = FakePrivd()
    nas.install(privd)
    settings = Settings(run_dir=str(tmp_path), state_dir=str(tmp_path / "state"), www_dir=str(CC / "frontend"),
                        catalog_dir=str(tmp_path / "none"), isos_dir=str(tmp_path / "none"), vnc_tokens_dir=str(tmp_path / "none"))
    app = create_app(settings, privd=privd, font_lister=lambda p: [], libvirt=FakeLibvirt())
    with TestClient(app, base_url=ORIGIN) as client:
        yield types.SimpleNamespace(app=app, client=client, nas=nas, tmp=tmp_path, mounted=mounted,
                                    h={"Authorization": f"Bearer {app.tokens.token}"})


def finish(w, job):
    lines = []
    protocols = ["nimbus.v1", f"nimbus.token.{w.app.tokens.token}"]
    with w.client.websocket_connect(f"{WS}/ws/jobs/{job}", subprotocols=protocols, headers={"origin": ORIGIN}) as ws:
        while True:
            m = ws.receive_json()
            if m["type"] == "done":
                return m, lines
            lines.append(m["line"])


def job(w, method, path, expect_ok=True, **kw):
    r = getattr(w.client, method)(path, headers=w.h, **kw)
    assert r.status_code == 200, r.text
    done, lines = finish(w, r.json()["job"])
    assert done["ok"] is expect_ok, (done, lines)
    return done


def verbs(w):
    return [v for v, _ in w.nas.calls if v not in ("disk.list", "smbuser.list")]


def saved_shares(w):
    return json.loads((w.tmp / "state/shares.json").read_text())["shares"]


# --- storage --------------------------------------------------------------------------------------
def test_overview(w):
    r = w.client.get("/api/storage", headers=w.h).json()
    assert r["pool"]["mode"] == "single" and r["live"] is False and r["missing"] == [] and r["parity"] is None
    w.nas.fstab += [{"kind": "disk", "role": "parity", "label": "parity1", "uuid": "u2", "fs": "xfs", "present": False},
                    {"kind": "pool", "branches": ["disk1"], "policy": "epmfs"}]
    w.nas.parity_configured = True
    r = w.client.get("/api/storage", headers=w.h).json()
    assert r["pool"]["mode"] == "parity" and r["pool"]["data"] == ["disk1"] and r["pool"]["parity"] == ["parity1"]
    assert r["pool"]["policy"] == "epmfs" and r["missing"] == ["parity1"] and r["parity"]["diff"]["unprotected"] == 3
    w.nas.parity_configured = False
    assert w.client.get("/api/storage", headers=w.h).json()["pool"]["mode"] == "pooled"
    assert w.client.get("/api/storage").status_code == 401


def test_format_job(w):
    job(w, "post", "/api/storage/disks/format", json={"device": "/dev/sdb", "serial": "SN-sdb", "label": "disk2"})
    assert ("disk.format", {"device": "/dev/sdb", "serial": "SN-sdb", "fs": "xfs", "label": "disk2", "role": "data"}) in w.nas.calls
    r = w.client.post("/api/storage/disks/format", json={"device": "/dev/sdc", "serial": "x", "label": "Disk 3"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "label"
    r = w.client.post("/api/storage/disks/format", json={"device": "/dev/sdc", "serial": "x", "label": "d3", "fs": "ntfs"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "fs"
    w.nas.fail["disk.format"] = ("invalid_argument", "That is not the serial number of this disk.", "serial")
    done = job(w, "post", "/api/storage/disks/format", expect_ok=False, json={"device": "/dev/sdc", "serial": "x", "label": "d3"})
    assert done["message"] == "That is not the serial number of this disk."


def test_live_session_refuses_changes(w):
    w.nas.live = True
    for path, body in (("/api/storage/disks/format", {"device": "/dev/sdb", "serial": "SN-sdb", "label": "d2"}),
                       ("/api/storage/wizard", {"mode": "single", "system": True, "shares": [], "user": {}})):
        r = w.client.post(path, json=body, headers=w.h)
        assert r.status_code == 409 and "Install NimbusOS" in r.json()["message"]
    r = w.client.put("/api/storage/pool", json={"mode": "pooled", "data": ["disk1"]}, headers=w.h)
    assert r.status_code == 409


def test_unmount_and_pool_protect_shares(w):
    create_share(w, name="films", location="disk:disk1")
    r = w.client.post("/api/storage/disks/disk1/unmount", headers=w.h)
    assert r.status_code == 409 and "films" in r.json()["message"]
    w.nas.fstab.append({"kind": "pool", "branches": ["disk1"], "policy": "mfs"})
    w.mounted.add(S.POOL)
    create_share(w, name="media", location="pool")
    r = w.client.put("/api/storage/pool", json={"mode": "single"}, headers=w.h)
    assert r.status_code == 409 and r.json()["field"] == "mode" and "media" in r.json()["message"]
    r = w.client.put("/api/storage/pool", json={"mode": "pooled", "data": []}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "data"
    w.nas.calls.clear()
    job(w, "put", "/api/storage/pool", json={"mode": "pooled", "policy": "epmfs", "data": ["disk1", "disk2"]})
    assert verbs(w) == ["pool.apply", "share.apply"]                                     # shares follow the pool
    assert next(a for v, a in w.nas.calls if v == "pool.apply") == {"mode": "pooled", "policy": "epmfs",
                                                                     "data": ["disk1", "disk2"], "parity": []}


def test_parity_sync_and_smart(w):
    assert "Parity sync started" in w.client.post("/api/storage/parity/sync", headers=w.h).json()["message"]
    assert w.client.post("/api/storage/disks/smart", json={"device": "/dev/sda"}, headers=w.h).json() == {"passed": True}


# --- the wizard ------------------------------------------------------------------------------------
WIZ = {"mode": "parity", "disks": [{"device": "/dev/sdb", "serial": "SN-sdb", "role": "data"},
                                   {"device": "/dev/sdc", "serial": "SN-sdc", "role": "parity"}],
       "existing": ["disk1"], "shares": ["media", "timemachine"], "user": {"name": "bob", "password": "c0rrect-H0rse"}}


@pytest.mark.parametrize("changes, field", [
    ({"disks": [{"device": "/dev/sdb", "serial": "wrong", "role": "data"}]}, "disks.0.serial"),
    ({"disks": [{"device": "/dev/nvme0n1", "serial": "SN-nvme0n1", "role": "data"}]}, "disks.0"),
    ({"disks": [{"device": "/dev/sda", "serial": "SN-sda", "role": "data"}]}, "disks.0"),
    ({"mode": "pooled"}, "disks.1"),                                    # a parity disk outside parity mode
    ({"disks": [{"device": "/dev/sdb", "serial": "SN-sdb", "role": "data"}]}, "disks"),   # parity mode, no parity
    ({"shares": ["media", "photos"]}, "shares"),
    ({"user": {"name": "Bob", "password": "c0rrect-H0rse"}}, "user.name"),
    ({"user": {"name": "bob", "password": "short"}}, "user.password"),
    ({"mode": "single", "disks": [{"device": "/dev/sdb", "serial": "SN-sdb", "role": "data"}]}, "disks"),
    ({"existing": ["disk9"]}, "existing"),
    ({"mode": "raid5"}, "mode"),
])
def test_wizard_checks(w, changes, field):
    r = w.client.post("/api/storage/wizard", json={**WIZ, **changes}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == field, r.text
    assert verbs(w) == []


def test_wizard_disk_without_serial_needs_its_name(w):
    r = w.client.post("/api/storage/wizard", json={**WIZ, "mode": "pooled", "existing": [],
                                                    "disks": [{"device": "/dev/sdd", "serial": "", "role": "data"}]}, headers=w.h)
    assert r.status_code == 422 and "(sdd)" in r.json()["message"]
    job(w, "post", "/api/storage/wizard", json={**WIZ, "mode": "pooled", "existing": [],
                                                 "disks": [{"device": "/dev/sdd", "serial": "sdd", "role": "data"}]})


def test_wizard_pooled_with_parity(w):
    w.mounted.add(S.POOL)
    done = job(w, "post", "/api/storage/wizard", json=WIZ)
    assert done["message"] == "Your NAS is ready."
    assert verbs(w) == ["disk.format", "disk.format", "pool.apply", "smbuser.add", "share.apply", "samba.settings"]
    formats = [a for v, a in w.nas.calls if v == "disk.format"]
    assert [(f["device"], f["label"], f["role"], f["fs"]) for f in formats] == [("/dev/sdb", "disk2", "data", "xfs"),
                                                                                ("/dev/sdc", "parity1", "parity", "xfs")]
    assert next(a for v, a in w.nas.calls if v == "pool.apply") == {"mode": "parity", "policy": "mfs", "data": ["disk1", "disk2"],
                                                                     "parity": ["parity1"]}
    applied = {s["name"]: s for s in w.nas.applied["shares"]}
    assert applied["media"]["security"] == "secure" and applied["media"]["users"] == {"bob": "rw"}
    assert applied["timemachine"]["timemachine"]["enabled"] is True and applied["timemachine"]["security"] == "private"
    assert all(s["location"] == "pool" for s in applied.values())
    assert [s["path"] for s in saved_shares(w)] == [f"{S.POOL}/media", f"{S.POOL}/timemachine"]
    r = w.client.post("/api/storage/wizard", json=WIZ, headers=w.h)
    assert r.status_code == 409                                                          # shares exist now


def test_wizard_on_the_system_disk(w):
    job(w, "post", "/api/storage/wizard", json={"mode": "single", "system": True, "shares": ["documents"],
                                                 "user": {"name": "alice", "password": "c0rrect-H0rse"}})
    assert verbs(w) == ["smbuser.passwd", "share.apply", "samba.settings"]               # alice existed: new password
    assert saved_shares(w)[0]["location"] == "system"


# --- shares ----------------------------------------------------------------------------------------------
def create_share(w, expect=200, **body):
    spec = {"name": "media", "location": "system", "security": "private", "users": {"alice": "rw"}}
    spec.update(body)
    r = w.client.post("/api/shares", json=spec, headers=w.h)
    assert r.status_code == expect, r.text
    if expect == 200:
        done, _ = finish(w, r.json()["job"])
        assert done["ok"], done
    return r.json()


def test_create_and_list(w):
    create_share(w, recycle={"enabled": True, "days": 14})
    applied = w.nas.applied
    assert applied["settings"] == {"enabled": True, "workgroup": "WORKGROUP", "description": "NimbusOS", "netbios": False}
    assert applied["shares"][0]["recycle"] == {"enabled": True, "days": 14}
    os.makedirs(f"{S.SYSTEM_SHARES}/media", exist_ok=True)
    r = w.client.get("/api/shares", headers=w.h).json()
    s = r["shares"][0]
    host = socket.gethostname()
    assert s["path"] == f"{S.SYSTEM_SHARES}/media" and s["available"] is True and s["space"]["total"] > 0
    assert s["connect"] == {"windows": f"\\\\{host}\\media", "mac": f"smb://{host}.local/media"}
    assert r["users"][0]["name"] == "alice" and r["settings"]["workgroup"] == "WORKGROUP"
    assert w.client.get("/api/apps/options", headers=w.h).json()["shares"] == [{"name": "media", "path": f"{S.SYSTEM_SHARES}/media"}]
    assert w.client.get("/api/system/summary", headers=w.h).json()["counts"]["shares"] == 1


@pytest.mark.parametrize("body, field, text", [
    ({"name": "../x"}, "name", "Share names may use"),
    ({"users": {"mallory": "rw"}}, "users", "not an SMB user"),
    ({"location": "pool"}, "location", "The pool isn't mounted"),
    ({"location": "disk:disk7"}, "location", "The disk disk7 isn't mounted"),
    ({"security": "everyone"}, "security", "Public, Secure or Private"),
    ({"recycle": {"enabled": True, "days": 0}}, "recycle.days", "from 1 to 3650"),
])
def test_share_field_errors(w, body, field, text):
    r = create_share(w, expect=422, **body)
    assert r["field"] == field and text in r["message"]
    assert w.nas.applied is None


def test_names_are_unique_and_locations_fixed(w):
    create_share(w)
    assert create_share(w, expect=422, name="MEDIA")["field"] == "name"
    r = w.client.put("/api/shares/media", json={"location": "disk:disk1", "security": "private"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "location"
    assert w.client.put("/api/shares/nope", json={}, headers=w.h).status_code == 404


def test_a_failed_apply_saves_nothing(w):
    create_share(w)
    before = saved_shares(w)
    w.nas.fail["share.apply"] = ("failed", "testparm -s failed: Unknown parameter encountered", None)
    r = w.client.put("/api/shares/media", json={"location": "system", "security": "public"}, headers=w.h)
    done, _ = finish(w, r.json()["job"])
    assert done["ok"] is False and "Unknown parameter" in done["message"] and saved_shares(w) == before


def test_edit_and_delete(w):
    create_share(w)
    job(w, "put", "/api/shares/media", json={"location": "system", "security": "secure", "users": {"alice": "rw"}, "browseable": False})
    assert saved_shares(w)[0]["security"] == "secure" and saved_shares(w)[0]["browseable"] is False
    done = job(w, "delete", "/api/shares/media")
    assert f"Its files are still in {S.SYSTEM_SHARES}/media" in done["message"]
    assert saved_shares(w) == [] and "share.delete_data" not in verbs(w)
    create_share(w)
    job(w, "delete", "/api/shares/media?delete_data=true")
    assert ("share.delete_data", {"name": "media", "location": "system"}) in w.nas.calls


# --- users and settings -------------------------------------------------------------------------------------
def test_users(w):
    r = w.client.post("/api/smbusers", json={"name": "bob", "password": "short"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "password"
    assert w.client.post("/api/smbusers", json={"name": "bob", "password": "c0rrect-H0rse"}, headers=w.h).json() == \
        {"message": "SMB user 'bob' added."}
    r = w.client.post("/api/smbusers", json={"name": "bob", "password": "c0rrect-H0rse"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "name" and "already" in r.json()["message"]
    assert w.client.put("/api/smbusers/bob/password", json={"password": "an0ther-one"}, headers=w.h).status_code == 200
    assert w.client.post("/api/smbusers/test", json={"user": "bob", "password": "c0rrect-H0rse"}, headers=w.h).json()["ok"] is True
    create_share(w, users={"alice": "rw", "bob": "ro"})
    w.nas.calls.clear()
    assert "was removed" in w.client.delete("/api/smbusers/bob", headers=w.h).json()["message"]
    assert verbs(w) == ["share.apply", "smbuser.delete"] and saved_shares(w)[0]["users"] == {"alice": "rw"}


def test_settings(w):
    assert w.client.get("/api/settings", headers=w.h).json() == {
        "samba": {"enabled": True, "workgroup": "WORKGROUP", "description": "NimbusOS", "netbios": False},
        "alerts": {"hdd_temp_c": 50, "nvme_temp_c": 70}}
    r = w.client.put("/api/settings", json={"samba": {"workgroup": "BAD WORKGROUP NAME"}}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "samba.workgroup"
    r = w.client.put("/api/settings", json={"alerts": {"hdd_temp_c": 200}}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "alerts.hdd_temp_c"
    assert w.client.put("/api/settings", json={"alerts": {"hdd_temp_c": 45}}, headers=w.h).json()["alerts"]["hdd_temp_c"] == 45
    assert verbs(w) == []                                                                 # alerts only: nothing to apply
    r = w.client.put("/api/settings", json={"samba": {"netbios": True, "workgroup": "HOME"}}, headers=w.h).json()
    assert r["samba"]["netbios"] is True and verbs(w) == ["share.apply", "samba.settings"]
    assert w.nas.calls[-1][1] == {"enabled": True, "netbios": True}
    assert w.nas.applied["settings"]["workgroup"] == "HOME"
