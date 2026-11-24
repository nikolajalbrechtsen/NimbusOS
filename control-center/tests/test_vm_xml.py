# =============================================================================
# control-center/tests/test_vm_xml.py: the libvirt domain XML
# =============================================================================
# What: - the Linux preset: virtio devices, UEFI without Secure Boot, no TPM,
#         no Hyper-V, UTC clock;
#       - the Windows preset: Secure Boot with enrolled keys and SMM, TPM 2.0
#         (CRB with UEFI, TIS with BIOS), Hyper-V enlightenments, local
#         clock; virtio with a driver ISO, SATA and e1000e without;
#       - always: disk boots before the CD, VNC on 127.0.0.1 with the
#         password, a USB tablet, a balloon with statistics, the preset in
#         NimbusOS metadata; host-passthrough with KVM, none under TCG;
#       - values go through the XML writer (an "&" in a path comes back
#         unchanged); parse_domain reads VMs made by other tools too;
#       - every combination passes libvirt's own schema (virt-xml-validate)
#         where libvirt-clients is installed.
# Why:  spec Tab B ("Generate the libvirt domain XML in Python"); NOTE-67.
# =============================================================================

import itertools
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from nimbus_control import vm_xml


def spec(**changes):
    s = {"name": "web1", "uuid": "0b1c3d2e-4f50-4a6b-8c7d-9e0f1a2b3c4d", "preset": "linux", "vcpus": 2, "memory_mib": 2048,
         "firmware": "uefi", "disk_path": "/var/lib/nimbus-vms/images/web1.qcow2", "iso": "/var/lib/nimbus-vms/isos/debian.iso",
         "drivers_iso": None, "network": {"mode": "nat"}, "vnc_password": "Ab3dE6gH", "created": "2026-11-18T10:00:00+00:00"}
    s.update(changes)
    return s


def build(kvm=True, **changes):
    xml = vm_xml.build_domain(spec(**changes), kvm=kvm)
    return xml, ET.fromstring(xml)


def test_linux_preset():
    xml, d = build()
    assert d.get("type") == "kvm" and d.findtext("name") == "web1" and d.findtext("vcpu") == "2"
    assert d.find("memory").attrib == {"unit": "MiB"} and d.findtext("memory") == "2048"
    assert d.find("os").get("firmware") == "efi" and d.find("os/type").get("machine") == "q35"
    assert [(f.get("name"), f.get("enabled")) for f in d.findall("os/firmware/feature")] == [("secure-boot", "no")]
    assert d.find("features/hyperv") is None and d.find("features/smm") is None and d.find("devices/tpm") is None
    assert d.find("cpu").get("mode") == "host-passthrough" and d.find("clock").get("offset") == "utc"
    disk, cdrom = d.findall("devices/disk")
    # a file path, which libvirt's AppArmor helper can allow (NOTE-72)
    assert disk.get("type") == "file"
    assert disk.find("source").attrib == {"file": "/var/lib/nimbus-vms/images/web1.qcow2"}
    assert disk.find("target").attrib == {"dev": "vda", "bus": "virtio"} and disk.find("boot").get("order") == "1"
    assert disk.find("driver").attrib == {"name": "qemu", "type": "qcow2", "discard": "unmap"}
    assert cdrom.get("device") == "cdrom" and cdrom.find("source").get("file") == "/var/lib/nimbus-vms/isos/debian.iso"
    assert cdrom.find("target").attrib == {"dev": "sda", "bus": "sata"} and cdrom.find("readonly") is not None
    assert cdrom.find("boot").get("order") == "2"
    iface = d.find("devices/interface")
    assert iface.get("type") == "network" and iface.find("source").get("network") == "default"
    assert iface.find("model").get("type") == "virtio"
    assert d.find("devices/video/model").get("type") == "virtio" and d.find("devices/rng") is not None
    assert d.find("devices/input").attrib == {"type": "tablet", "bus": "usb"}
    assert d.find("devices/memballoon/stats").get("period") == "5"
    assert vm_xml.parse_domain(xml)["preset"] == "linux"


def test_vnc_is_local_with_a_password():
    _, d = build()
    g = d.find("devices/graphics")
    assert g.get("type") == "vnc" and g.get("passwd") == "Ab3dE6gH" and g.get("autoport") == "yes"
    assert g.find("listen").attrib == {"type": "address", "address": "127.0.0.1"}
    assert d.find("devices/graphics[@listen='0.0.0.0']") is None


def test_windows_preset_with_drivers():
    xml, d = build(preset="windows", drivers_iso="/var/lib/nimbus-vms/isos/virtio-win.iso")
    features = [(f.get("name"), f.get("enabled")) for f in d.findall("os/firmware/feature")]
    assert features == [("secure-boot", "yes"), ("enrolled-keys", "yes")]
    assert d.find("features/smm").get("state") == "on"
    assert [e.tag for e in d.find("features/hyperv")] == list(vm_xml.HYPERV)
    assert d.find("features/hyperv/spinlocks").get("retries") == "8191"
    assert d.find("clock").get("offset") == "localtime" and d.find("clock/timer[@name='hypervclock']") is not None
    tpm = d.find("devices/tpm")
    assert tpm.get("model") == "tpm-crb" and tpm.find("backend").attrib == {"type": "emulator", "version": "2.0"}
    disk, cd1, cd2 = d.findall("devices/disk")
    assert disk.find("target").get("bus") == "virtio"
    assert [c.find("target").get("dev") for c in (cd1, cd2)] == ["sda", "sdb"]
    assert cd2.find("source").get("file").endswith("virtio-win.iso") and cd2.find("boot") is None
    assert d.find("devices/interface/model").get("type") == "virtio"
    assert d.find("devices/video/model").get("type") == "vga" and d.find("devices/rng") is None
    info = vm_xml.parse_domain(xml)
    assert info["preset"] == "windows" and info["firmware"] == "uefi" and len(info["cdroms"]) == 2


def test_windows_without_drivers_uses_devices_setup_knows():
    _, d = build(preset="windows")
    disk, cdrom = d.findall("devices/disk")
    assert disk.find("target").attrib == {"dev": "sda", "bus": "sata"}
    assert cdrom.find("target").attrib == {"dev": "sdb", "bus": "sata"}
    assert d.find("devices/interface/model").get("type") == "e1000e"


def test_windows_with_bios():
    _, d = build(preset="windows", firmware="bios")
    assert d.find("os").get("firmware") is None and d.find("os/firmware") is None
    assert d.find("features/smm") is None and d.find("devices/tpm").get("model") == "tpm-tis"


def test_bridge_tcg_and_an_empty_cd_drive():
    xml, d = build(kvm=False, network={"mode": "bridge", "bridge": "br0"}, iso=None)
    assert d.get("type") == "qemu" and d.find("cpu") is None
    assert d.find("devices/interface").get("type") == "bridge" and d.find("devices/interface/source").get("bridge") == "br0"
    cdrom = d.findall("devices/disk")[1]
    assert cdrom.find("source") is None and cdrom.find("boot").get("order") == "2"
    assert vm_xml.parse_domain(xml)["network"] == {"mode": "bridge", "source": "br0"}


def test_values_are_escaped_by_the_xml_writer():
    path = "/var/lib/nimbus-vms/isos/A&B <1> \"x\".iso"
    xml, _ = build(iso=path)
    assert "A&amp;B &lt;1&gt;" in xml
    assert vm_xml.parse_domain(xml)["cdroms"][0]["path"] == path


def test_cdrom_xml_for_change_and_eject():
    eject = ET.tostring(vm_xml.cdrom_xml("sda", "sata", None, "2"), encoding="unicode")
    assert "<source" not in eject and 'order="2"' in eject and "<readonly" in eject
    insert = vm_xml.cdrom_xml("sdb", "sata", "/var/lib/nimbus-vms/isos/x.iso")
    assert insert.find("source").get("file") == "/var/lib/nimbus-vms/isos/x.iso" and insert.find("boot") is None


FOREIGN = """<domain type='kvm'>
  <name>old-vm</name><uuid>11111111-2222-3333-4444-555555555555</uuid>
  <memory unit='KiB'>1048576</memory><vcpu>1</vcpu>
  <os><type arch='x86_64' machine='pc-q35-9.2'>hvm</type>
    <loader readonly='yes' type='pflash'>/usr/share/OVMF/OVMF_CODE_4M.fd</loader></os>
  <devices>
    <disk type='file' device='disk'><source file='/var/lib/libvirt/images/old.qcow2'/><target dev='vda' bus='virtio'/></disk>
    <interface type='network'><source network='default'/></interface>
    <graphics type='spice' autoport='yes'/>
  </devices>
</domain>"""


def test_parse_a_vm_made_by_another_tool():
    info = vm_xml.parse_domain(FOREIGN)
    assert info["managed"] is False and info["preset"] is None and info["firmware"] == "uefi"
    assert info["disks"] == [{"dev": "vda", "bus": "virtio", "path": "/var/lib/libvirt/images/old.qcow2", "pool": None,
                              "volume": None}]
    assert info["vnc"] is None and info["cdroms"] == [] and info["network"] == {"mode": "nat", "source": "default"}


@pytest.mark.skipif(not shutil.which("virt-xml-validate"), reason="needs libvirt-clients (virt-xml-validate)")
@pytest.mark.parametrize("preset, firmware, drivers, network, kvm", [c for c in itertools.product(
    ("linux", "windows"), ("uefi", "bios"), (None, "/var/lib/nimbus-vms/isos/virtio-win.iso"),
    ({"mode": "nat"}, {"mode": "bridge", "bridge": "br0"}), (True, False)) if not (c[0] == "linux" and c[2])])
def test_libvirt_schema_accepts_every_combination(tmp_path, preset, firmware, drivers, network, kvm):
    xml, _ = build(kvm=kvm, preset=preset, firmware=firmware, drivers_iso=drivers, network=network)
    path = tmp_path / "domain.xml"
    path.write_text(xml)
    result = subprocess.run(["virt-xml-validate", str(path), "domain"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
