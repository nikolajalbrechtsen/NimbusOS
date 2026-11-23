# =============================================================================
# control-center/tests/test_vm_libvirt.py: VMs on a real libvirtd
# =============================================================================
# What: the real LibvirtApi and VmService against qemu:///system (KVM if the
#       container can use it, else TCG) and a real websockify:
#         - host facts; the nimbus-images pool is made on first use;
#         - a Linux UEFI VM and a Windows VM (Secure Boot firmware with
#           Microsoft's keys, swtpm TPM 2.0, Hyper-V) are created, start and
#           run; a legacy BIOS VM too;
#         - the Dashboard's statistics call: a running VM's state, vCPUs,
#           memory and resident size; CPU time moves; a paused VM's reason
#           (libvirt's own number, read back); a stopped VM in the same call;
#         - the CD is ejected and inserted while running;
#           reboot, ACPI shutdown (no OS answers it), force off; autostart;
#         - a console token reaches the VM's VNC server through websockify,
#           which offers VNC password authentication; a wrong token is refused;
#         - deleting with the disk removes the disk, the UEFI variables and
#           the TPM state.
#       Skipped unless NIMBUS_TEST_LIBVIRT=1 (set by
#       tests/integration/vm-libvirt.sh, which starts libvirtd).
# Why:  the fakes in test_vms.py follow libvirt's behaviour; this checks
#       that they do (docs/PLAN.md NOTE-67).
# =============================================================================

import asyncio
import os
import subprocess
import time

import pytest

from nimbus_control.iso_library import IsoLibrary
from nimbus_control.libvirt_api import LibvirtApi
from nimbus_control.vm_service import VmService

pytestmark = pytest.mark.skipif(os.environ.get("NIMBUS_TEST_LIBVIRT") != "1",
                                reason="needs a running libvirtd (tests/integration/vm-libvirt.sh)")
ISOS = "/var/lib/nimbus-vms/isos"
IMAGES = "/var/lib/nimbus-vms/images"
NVRAM = "/var/lib/libvirt/qemu/nvram"
SWTPM = "/var/lib/libvirt/swtpm"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tokens = tmp_path_factory.mktemp("tokens")
    with open(os.path.join(ISOS, "blank.iso"), "wb") as f:
        f.write(b"\0" * 1024 * 1024)
    lv = LibvirtApi("qemu:///system", IMAGES)
    svc = VmService(lv, IsoLibrary(ISOS, 2 ** 30), str(tokens), bridges=lambda: [])
    ws = subprocess.Popen(["websockify", "--token-plugin=TokenFile", f"--token-source={tokens}", "127.0.0.1:6080"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    yield svc, lv, tokens
    ws.terminate()
    for d in lv.domains():
        if d["name"].startswith("it-"):
            lv.undefine(d["name"])


def run(coro):
    return asyncio.run(coro)


def state(lv, name):
    return next(d for d in lv.domains() if d["name"] == name)


def wait_running(lv, name, seconds=20):
    for _ in range(seconds * 4):
        if state(lv, name)["state"] == "running":
            return True
        time.sleep(0.25)
    return False


def test_host(env):
    _, lv, _ = env
    host = lv.host()
    assert host["efi"] is True and host["threads"] >= 1 and host["memory"] > 0
    assert isinstance(host["kvm"], bool)
    assert lv.nat_network()["exists"] is True


def test_linux_uefi_vm(env):
    svc, lv, tokens = env
    r = run(svc.create({"name": "it-linux", "preset": "linux", "vcpus": 1, "memory_mib": 512, "disk_gib": 1,
                        "iso": "blank.iso", "network": {"mode": "nat"}}))
    assert r["started"] is True, r["message"]
    assert os.path.exists(os.path.join(IMAGES, "it-linux.qcow2"))
    assert wait_running(lv, "it-linux")
    vm = state(lv, "it-linux")
    assert vm["preset"] == "linux" and vm["firmware"] == "uefi" and vm["vnc"]["port"] >= 5900
    assert vm["disks"][0]["in_pool"] is True and vm["disks"][0]["capacity"] == 1024 ** 3
    now = lv.stats()[vm["uuid"]]                     # the Dashboard's one call per sample
    assert now["name"] == "it-linux" and now["state"] == "running" and now["active"] and not now["problem"]
    assert now["vcpus"] == 1 and now["memory"] == 512 * 2 ** 20 and now["rss"] > 0
    time.sleep(1)
    assert lv.stats()[vm["uuid"]]["cpu_time"] > now["cpu_time"]
    virsh = ["virsh", "-c", "qemu:///system"]
    subprocess.run([*virsh, "suspend", "it-linux"], check=True, capture_output=True)
    paused = lv.stats()[vm["uuid"]]                  # libvirt's own reason number, not an alert
    assert (paused["state"], paused["reason"], paused["active"], paused["problem"]) == ("paused", "paused by the user", True, False)
    subprocess.run([*virsh, "resume", "it-linux"], check=True, capture_output=True)


def test_console_through_websockify(env):
    import websockets.sync.client as wsc
    svc, lv, tokens = env
    console = run(svc.console("it-linux"))
    assert len(console["password"]) == 8
    with wsc.connect(f"ws://127.0.0.1:6080{console['path']}", subprotocols=["binary"]) as ws:
        assert ws.recv(timeout=10) == b"RFB 003.008\n"
        ws.send(b"RFB 003.008\n")
        types = ws.recv(timeout=10)
        assert types[0] >= 1 and 2 in types[1:1 + types[0]]          # 2: VNC password authentication
    with pytest.raises(Exception):
        with wsc.connect("ws://127.0.0.1:6080/websockify?token=wrong", subprotocols=["binary"]) as ws:
            ws.recv(timeout=5)


def test_windows_vm(env):
    svc, lv, _ = env
    r = run(svc.create({"name": "it-windows", "preset": "windows", "vcpus": 2, "memory_mib": 1024, "disk_gib": 2,
                        "iso": "blank.iso", "drivers_iso": "blank.iso", "network": {"mode": "nat"}}))
    assert r["started"] is True, r["message"]
    assert wait_running(lv, "it-windows")
    xml = subprocess.run(["virsh", "-c", "qemu:///system", "dumpxml", "it-windows"], capture_output=True, text=True).stdout
    assert "OVMF_CODE_4M.ms.fd" in xml and "secure='yes'" in xml and "<tpm model='tpm-crb'>" in xml
    uid = state(lv, "it-windows")["uuid"]
    assert os.path.isdir(os.path.join(SWTPM, uid))


def test_bios_vm(env):
    svc, lv, _ = env
    r = run(svc.create({"name": "it-bios", "preset": "linux", "vcpus": 1, "memory_mib": 256, "disk_gib": 1,
                        "firmware": "bios", "network": {"mode": "nat"}}))
    assert r["started"] is True, r["message"]
    assert wait_running(lv, "it-bios") and state(lv, "it-bios")["firmware"] == "bios"


def test_cd_and_lifecycle(env):
    svc, lv, _ = env
    run(svc.change_media("it-linux", None))
    assert not any(c.get("path") for c in state(lv, "it-linux")["cdroms"])
    run(svc.change_media("it-linux", "blank.iso"))
    assert state(lv, "it-linux")["cdroms"][0]["path"] == os.path.join(ISOS, "blank.iso")
    assert "asked to restart" in run(svc.action("it-linux", "reboot"))
    assert run(svc.action("it-linux", "force-off")) == "'it-linux' was turned off."
    assert state(lv, "it-linux")["state"] == "stopped"
    off = lv.stats()[state(lv, "it-linux")["uuid"]]  # stopped VMs are in the same call, and not counted
    assert off["state"] == "stopped" and not off["active"] and not off["problem"]
    run(svc.set_autostart("it-linux", True))
    assert state(lv, "it-linux")["autostart"] is True
    assert run(svc.action("it-linux", "start")) == "'it-linux' started."
    assert wait_running(lv, "it-linux")
    assert "asked to shut down" in run(svc.action("it-linux", "shutdown"))   # the firmware may or may not obey
    time.sleep(1)
    if state(lv, "it-linux")["state"] != "stopped":
        run(svc.action("it-linux", "force-off"))
    assert run(svc.action("it-linux", "start")) == "'it-linux' started."


def test_delete_with_disk(env):
    svc, lv, _ = env
    uid = state(lv, "it-windows")["uuid"]
    r = run(svc.delete("it-windows", True))
    assert r["deleted"] == [os.path.join(IMAGES, "it-windows.qcow2")]
    assert not os.path.exists(os.path.join(IMAGES, "it-windows.qcow2"))
    assert not os.path.exists(os.path.join(NVRAM, "it-windows_VARS.fd"))
    assert not os.path.exists(os.path.join(SWTPM, uid))
    r = run(svc.delete("it-bios", False))
    assert r["kept"] == [os.path.join(IMAGES, "it-bios.qcow2")] and os.path.exists(r["kept"][0])
    lv.delete_volume("it-bios.qcow2")
