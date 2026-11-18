# =============================================================================
# nimbus_control/libvirt_api.py: talking to libvirt (qemu:///system)
# =============================================================================
# What: LibvirtApi wraps python3-libvirt. nimbus-control reaches libvirtd as
#       a member of the "libvirt" group (Debian's polkit rule allows that
#       group), so VMs, their disks and the disk pool are made by libvirtd
#       itself: nothing here needs root or privd.
#         host()           hardware virtualization (KVM) or TCG, UEFI
#                          support, host threads and memory
#         domains()        every VM with its state, settings, disks, CD-ROMs
#                          and VNC display (vm_xml.parse_domain)
#         stats()          one getAllDomainStats() call: CPU time, memory
#                          (balloon.rss), vCPUs
#         define(), start(), shutdown() (ACPI), force_off(), reboot(),
#         set_autostart(), undefine() (with its UEFI variables and TPM
#         state), change_cdrom(), vnc_target() (port and password)
#         ensure_pool(), create_volume(), delete_volume(): the "nimbus-images"
#         directory pool at /var/lib/nimbus-vms/images (qcow2)
#       Calls block, so the API runs them in worker threads. libvirt being
#       unreachable raises LibvirtUnavailable; a refused operation raises
#       VmError with libvirt's own explanation.
# Why:  spec Tab B; docs/PLAN.md §4.1 and NOTE-67.
# =============================================================================

import threading
import xml.etree.ElementTree as ET

from . import vm_xml

POOL = "nimbus-images"
UNAVAILABLE = "libvirt isn't running, so virtual machines can't be shown. Start it with: sudo systemctl start libvirtd"
STATES = {0: "unknown", 1: "running", 2: "running", 3: "paused", 4: "stopping", 5: "stopped", 6: "crashed",
          7: "suspended"}
# Why a paused VM is paused (virDomainPausedReason), in words.
PAUSED_REASONS = {1: "paused by the user", 3: "paused while saving", 5: "paused by a disk error (is the disk full?)",
                  6: "paused because its watchdog fired", 10: "paused because its disk space ran out",
                  11: "paused while starting", 13: "paused after a crash (guest panic)"}


class LibvirtUnavailable(Exception):
    pass


class VmError(Exception):
    pass


class VmNotFound(VmError):
    pass


class LibvirtApi:
    def __init__(self, uri="qemu:///system", images_dir="/var/lib/nimbus-vms/images"):
        self.uri = uri
        self.images_dir = images_dir
        self._conn = None
        self._lock = threading.Lock()
        self._host = None

    # --- connection ------------------------------------------------------------
    def _libvirt(self):
        try:
            import libvirt
        except ImportError as exc:
            raise LibvirtUnavailable("python3-libvirt is missing; reinstall nimbus-control.") from exc
        libvirt.registerErrorHandler(lambda ctx, err: None, None)      # no noise on stderr
        return libvirt

    def _conn_(self):
        libvirt = self._libvirt()
        with self._lock:
            if self._conn is not None:
                try:
                    if self._conn.isAlive():
                        return self._conn, libvirt
                except libvirt.libvirtError:
                    pass
                self._conn = None
            try:
                self._conn = libvirt.open(self.uri)
            except libvirt.libvirtError as exc:
                raise LibvirtUnavailable(UNAVAILABLE) from exc
            return self._conn, libvirt

    def _call(self, fn):
        conn, libvirt = self._conn_()
        try:
            return fn(conn, libvirt)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise VmNotFound(exc.get_error_message()) from None
            try:
                alive = conn.isAlive()
            except libvirt.libvirtError:
                alive = False
            if not alive:                       # libvirtd went away (stopped, restarted)
                self._conn = None
                raise LibvirtUnavailable(UNAVAILABLE) from exc
            raise VmError(exc.get_error_message()) from None

    def _dom(self, conn, name):
        return conn.lookupByName(name)

    # --- host --------------------------------------------------------------------------
    def host(self):
        if self._host is None:
            def work(conn, libvirt):
                try:
                    caps = conn.getDomainCapabilities(None, "x86_64", "q35", "kvm", 0)
                    kvm = True
                except libvirt.libvirtError:
                    caps = conn.getDomainCapabilities(None, "x86_64", "q35", "qemu", 0)
                    kvm = False
                firmware = {v.text for v in ET.fromstring(caps).findall("os/enum[@name='firmware']/value")}
                info = conn.getInfo()            # [model, memory MiB, cpus, ...]
                return {"kvm": kvm, "efi": "efi" in firmware, "threads": int(info[2]),
                        "memory": int(info[1]) * 1024 * 1024}
            self._host = self._call(work)
        return dict(self._host)

    def nat_network(self):
        """Whether libvirt's "default" NAT network is defined."""
        def work(conn, libvirt):
            try:
                net = conn.networkLookupByName("default")
                return {"exists": True, "active": bool(net.isActive())}
            except libvirt.libvirtError:
                return {"exists": False, "active": False}
        return self._call(work)

    # --- reading ---------------------------------------------------------------------------
    def count(self):
        return self._call(lambda conn, libvirt: len(conn.listAllDomains(0)))

    def domains(self):
        def work(conn, libvirt):
            pool = self._pool(conn, libvirt, create=False)
            result = []
            for dom in conn.listAllDomains(0):
                state, reason = dom.state()
                info = vm_xml.parse_domain(dom.XMLDesc(0))
                for disk in info["disks"]:
                    disk.update(self._disk_size(conn, libvirt, pool, disk))
                result.append({
                    "name": dom.name(), "uuid": dom.UUIDString(), "state": STATES.get(state, "unknown"),
                    "reason": PAUSED_REASONS.get(reason, "paused") if state == 3 else "",
                    "autostart": bool(dom.autostart()), "persistent": bool(dom.isPersistent()),
                    "vcpus": dom.info()[3], "memory": dom.maxMemory() * 1024, **info,
                })
            return result
        return self._call(work)

    def _disk_size(self, conn, libvirt, pool, disk):
        try:
            if disk.get("volume") and disk.get("pool"):
                vol = conn.storagePoolLookupByName(disk["pool"]).storageVolLookupByName(disk["volume"])
            elif disk.get("path"):
                vol = conn.storageVolLookupByPath(disk["path"])
            else:
                return {}
            _, capacity, allocation = vol.info()
            return {"path": vol.path(), "capacity": capacity, "allocation": allocation,
                    "in_pool": pool is not None and vol.storagePoolLookupByVolume().name() == POOL}
        except libvirt.libvirtError:
            return {}

    def stats(self):
        """{uuid: {cpu_time (ns), rss (bytes), vcpus, memory (bytes)}} for running VMs."""
        def work(conn, libvirt):
            kinds = (libvirt.VIR_DOMAIN_STATS_STATE | libvirt.VIR_DOMAIN_STATS_CPU_TOTAL
                     | libvirt.VIR_DOMAIN_STATS_BALLOON | libvirt.VIR_DOMAIN_STATS_VCPU)
            out = {}
            for dom, s in conn.getAllDomainStats(kinds, libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE):
                memory = s.get("balloon.current", 0) * 1024
                out[dom.UUIDString()] = {"cpu_time": s.get("cpu.time", 0), "vcpus": s.get("vcpu.current", 1) or 1,
                                         "rss": s.get("balloon.rss", 0) * 1024 or memory, "memory": memory}
            return out
        return self._call(work)

    def vnc_target(self, name):
        """(host, port, password) of a running VM's VNC display, or None."""
        def work(conn, libvirt):
            dom = self._dom(conn, name)
            if not dom.isActive():
                return None
            vnc = vm_xml.parse_domain(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE))["vnc"]
            if not vnc or vnc["port"] <= 0:
                return None
            host = vnc["listen"] if vnc["listen"] not in ("0.0.0.0", "::", "") else "127.0.0.1"
            return host, vnc["port"], vnc["password"]
        return self._call(work)

    # --- changing ---------------------------------------------------------------------------
    def define(self, xml):
        return self._call(lambda conn, libvirt: conn.defineXML(xml).name())

    def start(self, name):
        self._call(lambda conn, libvirt: self._dom(conn, name).create())

    def shutdown(self, name):
        self._call(lambda conn, libvirt: self._dom(conn, name).shutdown())

    def force_off(self, name):
        self._call(lambda conn, libvirt: self._dom(conn, name).destroy())

    def reboot(self, name):
        self._call(lambda conn, libvirt: self._dom(conn, name).reboot(0))

    def resume(self, name):
        self._call(lambda conn, libvirt: self._dom(conn, name).resume())

    def set_autostart(self, name, enabled):
        self._call(lambda conn, libvirt: self._dom(conn, name).setAutostart(1 if enabled else 0))

    def undefine(self, name):
        """Turn the VM off if needed and remove it, with its UEFI variables,
        TPM state and saved state. Disks are left alone."""
        def work(conn, libvirt):
            dom = self._dom(conn, name)
            if dom.isActive():
                dom.destroy()
            flags = (libvirt.VIR_DOMAIN_UNDEFINE_NVRAM | libvirt.VIR_DOMAIN_UNDEFINE_TPM
                     | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
                     | libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA)
            dom.undefineFlags(flags)
        self._call(work)

    def change_cdrom(self, name, dev, bus, path):
        """Put PATH (or nothing, to eject) into the CD-ROM drive DEV, now and
        for the next start."""
        def work(conn, libvirt):
            dom = self._dom(conn, name)
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG | (libvirt.VIR_DOMAIN_AFFECT_LIVE if dom.isActive() else 0)
            boot = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)).find(
                f"devices/disk[@device='cdrom']/target[@dev='{dev}']/../boot")
            xml = ET.tostring(vm_xml.cdrom_xml(dev, bus, path, boot.get("order") if boot is not None else None),
                              encoding="unicode")
            dom.updateDeviceFlags(xml, flags)
        self._call(work)

    # --- the disk pool --------------------------------------------------------------------------
    def _pool(self, conn, libvirt, create=True):
        try:
            pool = conn.storagePoolLookupByName(POOL)
        except libvirt.libvirtError:
            if not create:
                return None
            root = ET.Element("pool", {"type": "dir"})
            ET.SubElement(root, "name").text = POOL
            target = ET.SubElement(root, "target")
            ET.SubElement(target, "path").text = self.images_dir
            pool = conn.storagePoolDefineXML(ET.tostring(root, encoding="unicode"), 0)
            pool.setAutostart(1)
        if not pool.isActive():
            if not create:
                return None
            pool.create(0)
        return pool

    def ensure_pool(self):
        self._call(lambda conn, libvirt: self._pool(conn, libvirt).refresh(0))

    def pool_free(self):
        def work(conn, libvirt):
            pool = self._pool(conn, libvirt, create=False)
            return pool.info()[3] if pool is not None else None
        return self._call(work)

    def create_volume(self, base, size_bytes):
        """Make an empty qcow2 disk named BASE.qcow2 (or BASE-2.qcow2, ... if
        that name is taken, for example by a disk kept from a deleted VM).
        Returns (volume name, path)."""
        def work(conn, libvirt):
            pool = self._pool(conn, libvirt)
            pool.refresh(0)
            taken = set(pool.listVolumes())
            name, n = f"{base}.qcow2", 2
            while name in taken:
                name, n = f"{base}-{n}.qcow2", n + 1
            vol = ET.Element("volume")
            ET.SubElement(vol, "name").text = name
            ET.SubElement(vol, "capacity", {"unit": "bytes"}).text = str(size_bytes)
            ET.SubElement(vol, "allocation", {"unit": "bytes"}).text = "0"
            target = ET.SubElement(vol, "target")
            ET.SubElement(target, "format", {"type": "qcow2"})
            perms = ET.SubElement(target, "permissions")
            ET.SubElement(perms, "mode").text = "0600"
            created = pool.createXML(ET.tostring(vol, encoding="unicode"), 0)
            return created.name(), created.path()
        return self._call(work)

    def delete_volume(self, name):
        """Delete a disk from the nimbus-images pool (and nothing else)."""
        def work(conn, libvirt):
            self._pool(conn, libvirt).storageVolLookupByName(name).delete(0)
        self._call(work)
