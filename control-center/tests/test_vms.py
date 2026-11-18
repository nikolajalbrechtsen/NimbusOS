# =============================================================================
# control-center/tests/test_vms.py: the Virtual Machines API
# =============================================================================
# What: the real app with FakeLibvirt, a temporary ISO library and token
#       folder, and a fixed list of host bridges:
#         - the list (states, presets, usage from two samples, the CD, VMs
#           made by other tools), libvirt down, no KVM;
#         - Create VM: the disk, the domain, autostart and start; every
#           invalid setting refused with its field and nothing made; a
#           refused domain removes its disk again; a VM that doesn't start
#           is kept and says so; disk names never reuse a kept disk's;
#         - actions with clear refusals; options (CD, autostart); delete
#           with and without disks, never a disk outside the pool;
#         - consoles: one token file per VM (content, mode, replaced on the
#           next console, removed when the VM stops or is deleted, all
#           removed at startup); none for a stopped VM;
#         - the ISO library over HTTP: chunked upload, used-by, delete
#           refused while a VM uses the ISO;
#         - the token is needed, noVNC is served, the CSP allows websockify,
#           the sidebar counts VMs.
# Why:  spec Tab B; docs/PLAN.md NOTE-16, NOTE-67.
# =============================================================================

import os
import stat
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from conftest import CC, FakeLibvirt, FakePrivd
from nimbus_control import vm_service
from nimbus_control.app import create_app
from nimbus_control.config import Settings
from test_vm_xml import FOREIGN

ORIGIN = "http://127.0.0.1:7777"
GIB = 1024 ** 3


@pytest.fixture
def w(tmp_path):
    for d in ("isos", "tokens", "novnc/core", "state"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "novnc/core/rfb.js").write_text("export default class RFB {}\n")
    (tmp_path / "tokens" / "stale").write_text("old: 127.0.0.1:5999\n")
    (tmp_path / "isos" / "debian.iso").write_bytes(b"\0" * 2048)
    (tmp_path / "isos" / "virtio-win.iso").write_bytes(b"\0" * 1024)
    lv = FakeLibvirt()
    settings = Settings(run_dir=str(tmp_path), state_dir=str(tmp_path / "state"), www_dir=str(CC / "frontend"),
                        isos_dir=str(tmp_path / "isos"), vnc_tokens_dir=str(tmp_path / "tokens"),
                        novnc_dir=str(tmp_path / "novnc"), catalog_dir=str(tmp_path / "none"), iso_max_bytes=64 * 1024 ** 2)
    app = create_app(settings, privd=FakePrivd(), font_lister=lambda p: [], libvirt=lv, bridges=lambda: ["br0"])
    with TestClient(app, base_url=ORIGIN) as client:
        yield types.SimpleNamespace(app=app, client=client, lv=lv, tmp=tmp_path, tokens=tmp_path / "tokens",
                                    isos=tmp_path / "isos", h={"Authorization": f"Bearer {app.tokens.token}"},
                                    svc=app.app.state.vms)


def create(w, expect=200, **changes):
    body = {"name": "web1", "preset": "linux", "vcpus": 2, "memory_mib": 2048, "disk_gib": 32, "firmware": "uefi",
            "network": {"mode": "nat"}, "iso": "debian.iso", "start": True}
    body.update(changes)
    r = w.client.post("/api/vms", json=body, headers=w.h)
    assert r.status_code == expect, r.text
    return r.json()


def vms(w):
    return {v["name"]: v for v in w.client.get("/api/vms", headers=w.h).json()["vms"]}


def domain(w, name="web1"):
    return ET.fromstring(w.lv.vms[name]["xml"])


# --- list and host ---------------------------------------------------------------------
def test_startup_clears_old_console_tokens(w):
    assert os.listdir(w.tokens) == []


def test_list_and_usage(w, monkeypatch):
    create(w)
    clock = [1000.0]
    monkeypatch.setattr(vm_service.time, "monotonic", lambda: clock[0])
    first = vms(w)["web1"]
    assert first["state"] == "running" and first["preset"] == "linux" and first["firmware"] == "uefi"
    assert first["vcpus"] == 2 and first["memory"] == 2 * GIB and first["iso"] == "debian.iso" and not first["iso_missing"]
    assert first["disks"] == [{"path": "/var/lib/nimbus-vms/images/web1.qcow2", "capacity": 32 * GIB, "allocation": 0,
                               "in_pool": True}]
    assert first["console"] is True and first["cpu_percent"] is None and first["memory_used"] == 512 * 1024 ** 2
    w.lv.vms["web1"]["cpu_time"] += int(1.5e9)                 # 1.5 s of CPU in 1 s on 2 vCPUs
    clock[0] += 1.0
    second = vms(w)["web1"]
    assert second["cpu_percent"] == pytest.approx(75.0) and second["cpu_host_percent"] == pytest.approx(18.75)
    r = w.client.get("/api/vms", headers=w.h).json()
    assert r["host"] == {"kvm": True, "efi": True, "threads": 8, "memory": 16 * GIB} and r["libvirt"]["ok"] is True


def test_libvirt_down_and_no_kvm(w):
    w.lv.available = False
    r = w.client.get("/api/vms", headers=w.h).json()
    assert r["vms"] == [] and r["libvirt"]["ok"] is False and "systemctl start libvirtd" in r["libvirt"]["message"]
    r = w.client.get("/api/vms/options", headers=w.h)
    assert r.status_code == 503
    assert w.client.get("/api/system/summary", headers=w.h).json()["counts"]["vms"] is None
    w.lv.available = True
    w.lv.host_info["kvm"] = False
    create(w)
    assert domain(w).get("type") == "qemu" and domain(w).find("cpu") is None
    assert w.client.get("/api/vms", headers=w.h).json()["host"]["kvm"] is False


def test_options(w):
    r = w.client.get("/api/vms/options", headers=w.h).json()
    assert r["isos"] == ["debian.iso", "virtio-win.iso"] and r["bridges"] == ["br0"]
    assert r["nat"] == {"exists": True, "active": True} and r["free_bytes"] == 200 * GIB
    assert r["defaults"]["windows"] == {"vcpus": 4, "memory_mib": 8192, "disk_gib": 80, "firmware": "uefi"}


def add_foreign(w):
    """A VM made with virt-manager: no NimbusOS metadata, a disk outside the pool, SPICE."""
    w.lv.vms["old"] = {"xml": FOREIGN.replace("old-vm", "old"), "state": "stopped", "autostart": True,
                       "uuid": "11111111-2222-3333-4444-555555555555", "cpu_time": 0}


def test_a_vm_from_another_tool_is_listed(w):
    add_foreign(w)
    old = vms(w)["old"]
    assert old["preset"] is None and old["managed"] is False and old["console"] is False and old["autostart"] is True
    assert old["disks"][0]["in_pool"] is False


# --- create ------------------------------------------------------------------------------------
def test_create_linux(w):
    r = create(w, autostart=True)
    assert r == {"name": "web1", "disk": "/var/lib/nimbus-vms/images/web1.qcow2", "started": True,
                 "message": "'web1' was created and started."}
    assert w.lv.volumes == {"web1.qcow2": 32 * GIB}
    d = domain(w)
    assert d.get("type") == "kvm" and d.findtext("vcpu") == "2" and d.findtext("memory") == "2048"
    assert d.find("devices/disk[@device='cdrom']/source").get("file") == str(w.isos / "debian.iso")
    assert len(d.find("devices/graphics").get("passwd")) == 8
    assert w.lv.vms["web1"]["autostart"] is True and w.lv.vms["web1"]["state"] == "running"


def test_create_windows_with_drivers_on_a_bridge(w):
    create(w, name="win11", preset="windows", vcpus=4, memory_mib=8192, disk_gib=80, drivers_iso="virtio-win.iso",
           network={"mode": "bridge", "bridge": "br0"}, start=False)
    d = domain(w, "win11")
    assert d.find("devices/tpm") is not None and d.find("features/hyperv") is not None
    assert [c.find("source").get("file") for c in d.findall("devices/disk[@device='cdrom']")] == [
        str(w.isos / "debian.iso"), str(w.isos / "virtio-win.iso")]
    assert d.find("devices/interface/source").get("bridge") == "br0"
    assert w.lv.vms["win11"]["state"] == "stopped"


INVALID = [
    ({"name": "my vm"}, "name", "VM names may use"),
    ({"name": "../x"}, "name", "VM names may use"),
    ({"preset": "macos"}, "preset", "Linux or Windows"),
    ({"vcpus": 0}, "vcpus", "from 1 to 8"),
    ({"vcpus": 9}, "vcpus", "from 1 to 8"),
    ({"vcpus": "2"}, "vcpus", "whole number"),
    ({"vcpus": True}, "vcpus", "whole number"),
    ({"memory_mib": 128}, "memory_mib", "from 256 to 16384"),
    ({"memory_mib": 20000}, "memory_mib", "from 256 to 16384"),
    ({"disk_gib": 0}, "disk_gib", "from 1 to 65536"),
    ({"firmware": "coreboot"}, "firmware", "UEFI or legacy BIOS"),
    ({"network": {"mode": "macvtap"}}, "network", "NAT or a bridge"),
    ({"network": {"mode": "bridge", "bridge": "br9"}}, "network.bridge", "no network bridge named 'br9'"),
    ({"network": {"mode": "bridge", "bridge": "br0; rm"}}, "network.bridge", "Not a network bridge name"),
    ({"iso": "missing.iso"}, "iso", "no ISO named 'missing.iso'"),
    ({"iso": "../../etc/shadow.iso"}, "iso", "ISO file names may use"),
    ({"drivers_iso": "virtio-win.iso"}, "drivers_iso", "only for Windows"),
    ({"preset": "windows", "drivers_iso": "nope.iso"}, "drivers_iso", "no ISO named 'nope.iso'"),
]


@pytest.mark.parametrize("changes, field, text", INVALID)
def test_invalid_settings_are_refused_with_the_field(w, changes, field, text):
    r = create(w, expect=422, **changes)
    assert r["field"] == field and text in r["message"]
    assert w.lv.volumes == {} and w.lv.vms == {}


def test_more_refusals(w):
    create(w)
    assert create(w, expect=422)["field"] == "name"                       # the name is taken
    w.lv.host_info["efi"] = False
    r = create(w, expect=422, name="x")
    assert r["field"] == "firmware" and "ovmf" in r["message"]
    w.lv.nat = False
    r = create(w, expect=422, name="y", firmware="bios")
    assert r["field"] == "network" and "libvirt-daemon-config-network" in r["message"]
    assert list(w.lv.volumes) == ["web1.qcow2"]


def test_a_refused_domain_leaves_no_disk(w):
    w.lv.fail["define"] = "XML error: something libvirt dislikes"
    r = create(w, expect=409)
    assert "libvirt refused: XML error" in r["message"] and w.lv.volumes == {} and w.lv.vms == {}


def test_a_vm_that_does_not_start_is_kept(w):
    w.lv.fail["start"] = "unsupported configuration: Domain requires KVM"
    r = create(w)
    assert r["started"] is False and "was created, but it didn't start: libvirt refused: unsupported" in r["message"]
    assert "web1" in w.lv.vms and "web1.qcow2" in w.lv.volumes


def test_disk_names_never_reuse_a_kept_disk(w):
    create(w)
    w.client.delete("/api/vms/web1", headers=w.h)                           # disk kept
    r = create(w)
    assert r["disk"].endswith("/web1-2.qcow2") and set(w.lv.volumes) == {"web1.qcow2", "web1-2.qcow2"}


# --- lifecycle -----------------------------------------------------------------------------------
def act(w, action, name="web1"):
    return w.client.post(f"/api/vms/{name}/actions/{action}", headers=w.h)


def test_actions(w):
    create(w, start=False)
    assert act(w, "shutdown").status_code == 409 and "isn't running" in act(w, "shutdown").json()["message"]
    assert act(w, "start").json() == {"message": "'web1' started."}
    assert act(w, "start").json()["message"] == "'web1' is already running."
    assert "If its system doesn't react, use Force off." in act(w, "shutdown").json()["message"]
    assert act(w, "reboot").json() == {"message": "'web1' was asked to restart."}
    assert act(w, "resume").status_code == 409
    assert act(w, "force-off").json() == {"message": "'web1' was turned off."}
    assert w.lv.vms["web1"]["state"] == "stopped"
    w.lv.vms["web1"]["state"] = "paused"
    assert act(w, "resume").json()["message"] == "'web1' continues."
    assert act(w, "explode").status_code == 404
    assert act(w, "start", "nope").status_code == 404 and act(w, "start", "a b").status_code == 404
    assert [c[0] for c in w.lv.calls if c[0] in ("start", "shutdown", "force_off", "reboot", "resume")] == [
        "start", "shutdown", "reboot", "force_off", "resume"]


def test_options_cd_and_autostart(w):
    create(w)
    r = w.client.put("/api/vms/web1/media", json={"iso": "virtio-win.iso"}, headers=w.h)
    assert r.json()["message"] == "'virtio-win.iso' is in web1's CD drive."
    cd = domain(w).find("devices/disk[@device='cdrom']")
    assert cd.find("source").get("file") == str(w.isos / "virtio-win.iso") and cd.find("boot").get("order") == "2"
    assert w.client.put("/api/vms/web1/media", json={"iso": None}, headers=w.h).json()["message"] == \
        "The CD was ejected from 'web1'."
    assert domain(w).find("devices/disk[@device='cdrom']/source") is None
    r = w.client.put("/api/vms/web1/media", json={"iso": "../../etc/passwd"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "iso"
    assert w.client.put("/api/vms/web1/autostart", json={"enabled": True}, headers=w.h).json()["message"] == \
        "'web1' starts when the computer starts."
    assert w.lv.vms["web1"]["autostart"] is True
    assert w.client.put("/api/vms/web1/autostart", json={"enabled": "yes"}, headers=w.h).status_code == 422
    assert w.client.put("/api/vms/web1/autostart", content=b"{not json", headers=w.h).status_code == 422


def test_delete_keeps_disks_unless_asked(w):
    create(w)
    w.client.post("/api/vms/web1/console", headers=w.h)
    r = w.client.delete("/api/vms/web1", headers=w.h).json()
    assert r["message"] == "'web1' was deleted. Kept: /var/lib/nimbus-vms/images/web1.qcow2."
    assert w.lv.vms == {} and "web1.qcow2" in w.lv.volumes and os.listdir(w.tokens) == []
    create(w, name="web2")
    r = w.client.delete("/api/vms/web2?delete_disks=true", headers=w.h).json()
    assert r["deleted"] == ["/var/lib/nimbus-vms/images/web2.qcow2"] and "web2.qcow2" not in w.lv.volumes
    assert w.client.delete("/api/vms/web2", headers=w.h).status_code == 404


def test_delete_never_touches_disks_outside_the_pool(w):
    add_foreign(w)
    r = w.client.delete("/api/vms/old?delete_disks=true", headers=w.h).json()
    assert r["deleted"] == [] and r["kept"] == ["/var/lib/libvirt/images/old.qcow2"]
    assert "only disks in the NimbusOS disk folder are deleted" in r["message"]
    assert not any(c[0] == "delete_volume" for c in w.lv.calls)


# --- consoles -------------------------------------------------------------------------------------
def test_console_tokens(w):
    create(w)
    uid = w.lv.vms["web1"]["uuid"]
    r = w.client.post("/api/vms/web1/console", headers=w.h).json()
    token = r["path"].split("token=")[1]
    assert r["path"].startswith("/websockify?token=") and r["port"] == 6080 and len(token) >= 32
    assert r["password"] == domain(w).find("devices/graphics").get("passwd")
    file = w.tokens / uid
    assert file.read_text() == f"{token}: 127.0.0.1:5900\n" and stat.S_IMODE(file.stat().st_mode) == 0o640
    again = w.client.post("/api/vms/web1/console", headers=w.h).json()["path"].split("token=")[1]
    assert again != token and file.read_text() == f"{again}: 127.0.0.1:5900\n" and os.listdir(w.tokens) == [uid]
    act(w, "force-off")
    assert w.client.post("/api/vms/web1/console", headers=w.h).status_code == 409
    vms(w)                                                                   # listing removes tokens of stopped VMs
    assert os.listdir(w.tokens) == []


# --- the ISO library over HTTP ---------------------------------------------------------------------
def test_iso_upload_and_delete(w):
    data = os.urandom(3 * 1024 * 1024 + 5)
    begin = w.client.post("/api/isos/uploads", json={"name": "alpine.iso", "size": len(data)}, headers=w.h).json()
    assert begin["chunk_bytes"] == 8 * 1024 * 1024 and begin["received"] == 0
    step = 1024 * 1024
    for offset in range(0, len(data), step):
        r = w.client.put(f"/api/isos/uploads/{begin['id']}?offset={offset}", content=data[offset:offset + step],
                         headers={**w.h, "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    assert w.client.put(f"/api/isos/uploads/{begin['id']}", content=b"x", headers=w.h).status_code == 422   # no offset
    r = w.client.post(f"/api/isos/uploads/{begin['id']}/finish", headers=w.h).json()
    assert r == {"name": "alpine.iso", "size": len(data), "message": "'alpine.iso' is in the ISO library."}
    assert (w.isos / "alpine.iso").read_bytes() == data
    create(w, iso="alpine.iso")
    listing = w.client.get("/api/isos", headers=w.h).json()
    assert [(i["name"], i["used_by"]) for i in listing["isos"]] == [
        ("alpine.iso", ["web1"]), ("debian.iso", []), ("virtio-win.iso", [])]
    assert listing["path"] == str(w.isos) and listing["max_bytes"] == 64 * 1024 ** 2
    r = w.client.delete("/api/isos/alpine.iso", headers=w.h)
    assert r.status_code == 409 and "Eject it there first" in r.json()["message"]
    assert w.client.delete("/api/isos/debian.iso", headers=w.h).json()["message"] == "'debian.iso' was deleted."
    vm = vms(w)["web1"]
    assert vm["iso"] == "alpine.iso"
    r = w.client.post("/api/isos/uploads", json={"name": "../x.iso", "size": 10}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "name"


def test_a_vm_whose_iso_was_deleted_says_so(w):
    create(w)
    (w.isos / "debian.iso").unlink()
    vm = vms(w)["web1"]
    assert vm["iso"] == "debian.iso" and vm["iso_missing"] is True


# --- security and the shell -------------------------------------------------------------------------
def test_token_novnc_csp_and_count(w):
    for method, path in (("get", "/api/vms"), ("post", "/api/vms"), ("get", "/api/isos"), ("post", "/api/vms/x/console"),
                         ("post", "/api/isos/uploads"), ("put", "/api/isos/uploads/x?offset=0")):
        assert getattr(w.client, method)(path).status_code == 401, path
    r = w.client.get("/novnc/core/rfb.js")
    assert r.status_code == 200 and "class RFB" in r.text
    csp = r.headers["content-security-policy"]
    assert "ws://127.0.0.1:6080" in csp and "ws://localhost:6080" in csp and "ws://127.0.0.1:7777" in csp
    for page in ("/console.html", "/console.js", "/lib/vnc.js", "/views/vms.js"):
        assert w.client.get(page).status_code == 200, page
    create(w)
    assert w.client.get("/api/system/summary", headers=w.h).json()["counts"]["vms"] == 1
