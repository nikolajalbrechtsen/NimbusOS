# =============================================================================
# nimbus_control/vm_xml.py: libvirt domain XML, written and read in Python
# =============================================================================
# What: build_domain(spec, kvm) makes a VM's libvirt domain XML with
#       ElementTree (every value escaped by the XML writer, no string
#       templates, no virt-install):
#         - q35 machine; UEFI through libvirt's firmware selection
#           (<os firmware="efi">, from OVMF's descriptors) or SeaBIOS;
#         - Linux preset: virtio disk, network, video and RNG, UTC clock;
#         - Windows preset: Secure Boot with Microsoft's keys enrolled (SMM
#           on), TPM 2.0 emulated by swtpm, Hyper-V enlightenments, local
#           clock; virtio disk and network when a driver ISO (virtio-win) is
#           attached, otherwise SATA and e1000e so Setup finds them;
#         - the disk boots first, the CD-ROM second, so an installed VM boots
#           from its disk with the ISO still in the drive;
#         - VNC on 127.0.0.1 only, with a random password (NOTE-16), a USB
#           tablet so the console's pointer lines up, a memory balloon with
#           statistics, and NimbusOS metadata (the preset);
#         - kvm=False (no hardware virtualization) gives a TCG domain.
#       parse_domain(xml) reads back what the VM page shows: preset,
#       firmware, disks, CD-ROMs, network and the VNC display.
#       cdrom_xml() is the device for changing or ejecting a CD.
# Why:  spec Tab B: "Generate the libvirt domain XML in Python".
# =============================================================================

import xml.etree.ElementTree as ET

NS = "urn:nimbusos:vm:1"
PRESETS = ("linux", "windows")
FIRMWARES = ("uefi", "bios")
HYPERV = ("relaxed", "vapic", "spinlocks", "vpindex", "runtime", "synic", "stimer", "reset", "frequencies",
          "tlbflush", "ipi")

ET.register_namespace("nimbus", NS)


def _sub(parent, tag, attrib=None, text=None):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in (attrib or {}).items()})
    if text is not None:
        el.text = str(text)
    return el


def cdrom_xml(dev, bus, path=None, boot_order=None):
    """A CD-ROM drive, empty when PATH is None."""
    disk = ET.Element("disk", {"type": "file", "device": "cdrom"})
    _sub(disk, "driver", {"name": "qemu", "type": "raw"})
    if path:
        _sub(disk, "source", {"file": path})
    _sub(disk, "target", {"dev": dev, "bus": bus})
    _sub(disk, "readonly")
    if boot_order:
        _sub(disk, "boot", {"order": boot_order})
    return disk


def build_domain(spec, kvm=True):
    """SPEC: name, uuid, preset, vcpus, memory_mib, firmware, pool, volume,
    iso (path or None), drivers_iso (path or None), network ({"mode": "nat"}
    or {"mode": "bridge", "bridge": name}), vnc_password, created."""
    windows = spec["preset"] == "windows"
    uefi = spec["firmware"] == "uefi"
    virtio = not windows or bool(spec.get("drivers_iso"))

    dom = ET.Element("domain", {"type": "kvm" if kvm else "qemu"})
    _sub(dom, "name", text=spec["name"])
    _sub(dom, "uuid", text=spec["uuid"])
    meta = _sub(dom, "metadata")
    vm = _sub(meta, f"{{{NS}}}vm")
    _sub(vm, f"{{{NS}}}preset", text=spec["preset"])
    _sub(vm, f"{{{NS}}}created", text=spec["created"])
    _sub(dom, "memory", {"unit": "MiB"}, spec["memory_mib"])
    _sub(dom, "currentMemory", {"unit": "MiB"}, spec["memory_mib"])
    _sub(dom, "vcpu", {"placement": "static"}, spec["vcpus"])

    os_el = _sub(dom, "os", {"firmware": "efi"} if uefi else None)
    _sub(os_el, "type", {"arch": "x86_64", "machine": "q35"}, "hvm")
    if uefi:
        fw = _sub(os_el, "firmware")
        _sub(fw, "feature", {"enabled": "yes" if windows else "no", "name": "secure-boot"})
        if windows:
            _sub(fw, "feature", {"enabled": "yes", "name": "enrolled-keys"})

    features = _sub(dom, "features")
    _sub(features, "acpi")
    _sub(features, "apic")
    if windows:
        hv = _sub(features, "hyperv", {"mode": "custom"})
        for name in HYPERV:
            _sub(hv, name, {"state": "on", **({"retries": 8191} if name == "spinlocks" else {})})
    if uefi and windows:
        _sub(features, "smm", {"state": "on"})           # needed by Secure Boot
    if kvm:
        _sub(dom, "cpu", {"mode": "host-passthrough", "check": "none", "migratable": "on"})

    clock = _sub(dom, "clock", {"offset": "localtime" if windows else "utc"})
    _sub(clock, "timer", {"name": "rtc", "tickpolicy": "catchup"})
    _sub(clock, "timer", {"name": "pit", "tickpolicy": "delay"})
    _sub(clock, "timer", {"name": "hpet", "present": "no"})
    if windows:
        _sub(clock, "timer", {"name": "hypervclock", "present": "yes"})
    _sub(dom, "on_poweroff", text="destroy")
    _sub(dom, "on_reboot", text="restart")
    _sub(dom, "on_crash", text="destroy")
    pm = _sub(dom, "pm")
    _sub(pm, "suspend-to-mem", {"enabled": "no"})
    _sub(pm, "suspend-to-disk", {"enabled": "no"})

    dev = _sub(dom, "devices")
    disk = _sub(dev, "disk", {"type": "volume", "device": "disk"})
    _sub(disk, "driver", {"name": "qemu", "type": "qcow2", "discard": "unmap"})
    _sub(disk, "source", {"pool": spec["pool"], "volume": spec["volume"]})
    _sub(disk, "target", {"dev": "vda", "bus": "virtio"} if virtio else {"dev": "sda", "bus": "sata"})
    _sub(disk, "boot", {"order": 1})
    cdroms = ["sda", "sdb"] if virtio else ["sdb", "sdc"]
    dev.append(cdrom_xml(cdroms[0], "sata", spec.get("iso"), boot_order=2))
    if windows and spec.get("drivers_iso"):
        dev.append(cdrom_xml(cdroms[1], "sata", spec["drivers_iso"]))
    _sub(dev, "controller", {"type": "usb", "model": "qemu-xhci"})

    net = spec["network"]
    if net["mode"] == "bridge":
        iface = _sub(dev, "interface", {"type": "bridge"})
        _sub(iface, "source", {"bridge": net["bridge"]})
    else:
        iface = _sub(dev, "interface", {"type": "network"})
        _sub(iface, "source", {"network": "default"})
    _sub(iface, "model", {"type": "virtio" if virtio else "e1000e"})

    graphics = _sub(dev, "graphics", {"type": "vnc", "port": -1, "autoport": "yes", "passwd": spec["vnc_password"]})
    _sub(graphics, "listen", {"type": "address", "address": "127.0.0.1"})
    video = _sub(dev, "video")
    _sub(video, "model", {"type": "vga" if windows else "virtio"})
    _sub(dev, "input", {"type": "tablet", "bus": "usb"})
    if windows:
        tpm = _sub(dev, "tpm", {"model": "tpm-crb" if uefi else "tpm-tis"})
        _sub(tpm, "backend", {"type": "emulator", "version": "2.0"})
    else:
        rng = _sub(dev, "rng", {"model": "virtio"})
        _sub(rng, "backend", {"model": "random"}, "/dev/urandom")
    channel = _sub(dev, "channel", {"type": "unix"})
    _sub(channel, "target", {"type": "virtio", "name": "org.qemu.guest_agent.0"})
    balloon = _sub(dev, "memballoon", {"model": "virtio"})
    _sub(balloon, "stats", {"period": 5})
    ET.indent(dom)
    return ET.tostring(dom, encoding="unicode")


def parse_domain(xml):
    """What the VM page shows, from a domain's XML."""
    dom = ET.fromstring(xml)
    preset = dom.findtext(f"metadata/{{{NS}}}vm/{{{NS}}}preset")
    os_el = dom.find("os")
    uefi = os_el is not None and (os_el.get("firmware") == "efi" or os_el.find("loader") is not None)
    disks, cdroms = [], []
    for d in dom.findall("devices/disk"):
        src = d.find("source")
        target = d.find("target")
        entry = {"dev": target.get("dev") if target is not None else "",
                 "bus": target.get("bus") if target is not None else ""}
        if src is not None:
            entry.update({"path": src.get("file") or src.get("dev"), "pool": src.get("pool"), "volume": src.get("volume")})
        if d.get("device") == "cdrom":
            cdroms.append({**entry, "path": entry.get("path")})
        elif d.get("device") == "disk":
            disks.append(entry)
    network = None
    iface = dom.find("devices/interface")
    if iface is not None:
        src = iface.find("source")
        kind = iface.get("type")
        network = {"mode": "bridge" if kind == "bridge" else "nat" if kind == "network" else kind,
                   "source": (src.get("bridge") or src.get("network")) if src is not None else None}
    vnc = None
    g = dom.find("devices/graphics[@type='vnc']")
    if g is not None:
        listen = g.find("listen")
        vnc = {"port": int(g.get("port", "-1")),
               "listen": (listen.get("address") if listen is not None else None) or g.get("listen") or "127.0.0.1",
               "password": g.get("passwd")}
    return {"preset": preset if preset in PRESETS else None, "managed": preset is not None,
            "firmware": "uefi" if uefi else "bios", "disks": disks, "cdroms": cdroms, "network": network, "vnc": vnc}
