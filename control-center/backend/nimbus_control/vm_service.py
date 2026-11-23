# =============================================================================
# nimbus_control/vm_service.py: the Virtual Machines tab
# =============================================================================
# What: - GET /api/vms: every VM (also ones made with virt-manager) with its
#         state, preset, firmware, vCPUs, memory, disks, CD-ROM and live CPU
#         (% of its own vCPUs and of the host) and memory (balloon.rss)
#         from successive getAllDomainStats() samples; host facts (KVM or
#         not, UEFI support); libvirt down shows a message, not an error;
#       - GET /api/vms/options: ISOs, bridges, the NAT network, host limits
#         and each preset's defaults, for the Create VM wizard;
#       - POST /api/vms: validate everything with the field named, make the
#         qcow2 disk in the nimbus-images pool, define the domain from
#         vm_xml (removing the disk again if libvirt refuses it), set
#         autostart, start it;
#       - actions: start, shutdown (ACPI), force-off, reboot, resume;
#         autostart on or off; change or eject the CD;
#       - DELETE /api/vms/{name}: turns the VM off, removes it with its UEFI
#         variables and TPM state, and deletes its disks only when asked and
#         only those in the nimbus-images pool;
#       - consoles: POST /api/vms/{name}/console writes one websockify token
#         file per VM in /run/nimbus-control/vnc-tokens ("<token>:
#         127.0.0.1:<port>", atomically, 0640) and returns the WebSocket path
#         and the VNC password for the embedded noVNC. A new console replaces
#         the VM's old token; tokens of VMs that stopped are removed;
#       - the ISO library: list (with the VMs using each ISO), chunked
#         upload, delete (refused while a VM uses the ISO).
# Why:  spec Tab B; docs/PLAN.md §4.1, NOTE-16, NOTE-27 and NOTE-67.
# =============================================================================

import asyncio
import datetime
import os
import secrets
import string
import time
import uuid

from fastapi import APIRouter, Query, Request

from nimbus_common import validate as V

from . import vm_xml
from .errors import ApiError
from .libvirt_api import LibvirtUnavailable, VmError, VmNotFound

GIB = 1024 ** 3
MIB = 1024 ** 2
DEFAULTS = {"linux": {"vcpus": 2, "memory_mib": 2048, "disk_gib": 32, "firmware": "uefi"},
            "windows": {"vcpus": 4, "memory_mib": 8192, "disk_gib": 80, "firmware": "uefi"}}
MIN_MEMORY_MIB = 256
MAX_DISK_GIB = 64 * 1024
VNC_ALPHABET = string.ascii_letters + string.digits
BRIDGE_EXCLUDE = ("virbr", "docker", "br-")          # libvirt's and Docker's own bridges


def host_bridges(sys_class_net="/sys/class/net"):
    """Network bridges the user made (br0 and the like)."""
    try:
        names = os.listdir(sys_class_net)
    except OSError:
        return []
    return sorted(n for n in names if os.path.isdir(os.path.join(sys_class_net, n, "bridge"))
                  and not n.startswith(BRIDGE_EXCLUDE))


class VmService:
    def __init__(self, libvirt, isos, tokens_dir, bridges=host_bridges):
        self.libvirt = libvirt
        self.isos = isos
        self.tokens_dir = tokens_dir
        self.bridges = bridges
        self._samples = {}                   # uuid -> (monotonic time, cpu_time ns)
        self._threads = os.cpu_count() or 1

    async def _lv(self, method, *args):
        try:
            return await asyncio.to_thread(getattr(self.libvirt, method), *args)
        except LibvirtUnavailable as exc:
            raise ApiError(503, str(exc)) from None
        except VmNotFound:
            raise ApiError(404, f"There is no virtual machine named '{args[0] if args else ''}'.", "name") from None
        except VmError as exc:
            raise ApiError(409, f"libvirt refused: {exc}") from None

    # --- reading -------------------------------------------------------------------------
    def _usage(self, stats):
        now = time.monotonic()
        out = {}
        stats = {uid: s for uid, s in stats.items() if s.get("active", True)}
        for uid, s in stats.items():
            last = self._samples.get(uid)
            self._samples[uid] = (now, s["cpu_time"])
            cpu = host = None
            if last and now > last[0] and s["cpu_time"] >= last[1]:
                used = (s["cpu_time"] - last[1]) / ((now - last[0]) * 1e9)
                cpu = min(100.0, 100.0 * used / s["vcpus"])
                host = 100.0 * used / max(1, self._threads)
            out[uid] = {"cpu_percent": cpu, "cpu_host_percent": host, "memory_used": s["rss"]}
        for uid in set(self._samples) - set(stats):
            del self._samples[uid]
        return out

    async def list(self):
        try:
            host = await asyncio.to_thread(self.libvirt.host)
            domains = await asyncio.to_thread(self.libvirt.domains)
            stats = await asyncio.to_thread(self.libvirt.stats)
        except LibvirtUnavailable as exc:
            return {"vms": [], "host": None, "libvirt": {"ok": False, "message": str(exc)}}
        self._threads = host["threads"]
        usage = self._usage(stats)
        library = {i["name"] for i in self._iso_list()}
        vms = []
        for d in domains:
            iso = next((c["path"] for c in d["cdroms"] if c.get("path")), None)
            iso_name = os.path.basename(iso) if iso and os.path.dirname(iso) == self.isos.root else None
            vms.append({
                "name": d["name"], "uuid": d["uuid"], "state": d["state"], "reason": d["reason"],
                "preset": d["preset"], "managed": d["managed"], "firmware": d["firmware"], "vcpus": d["vcpus"],
                "memory": d["memory"], "autostart": d["autostart"], "network": d["network"],
                "disks": [{k: disk.get(k) for k in ("path", "capacity", "allocation", "in_pool")} for disk in d["disks"]],
                "iso": iso_name if iso_name in library else (iso and os.path.basename(iso)),
                "iso_missing": bool(iso) and iso_name not in library,
                "console": d["state"] in ("running", "paused") and bool(d["vnc"]),
                **usage.get(d["uuid"], {"cpu_percent": None, "cpu_host_percent": None, "memory_used": None}),
            })
        self._prune_tokens({d["uuid"] for d in domains if d["state"] in ("running", "paused")})
        return {"vms": vms, "host": host, "libvirt": {"ok": True, "message": None}}

    def _iso_list(self):
        try:
            return self.isos.list()
        except ApiError:
            return []

    async def options(self):
        host = await self._lv("host")
        nat = await self._lv("nat_network")
        free = await self._lv("pool_free")
        return {"host": host, "isos": [i["name"] for i in self._iso_list()], "bridges": self.bridges(),
                "nat": nat, "defaults": DEFAULTS, "images_dir": self.libvirt.images_dir,
                "free_bytes": free if free is not None else self._images_free()}

    def _images_free(self):
        try:
            st = os.statvfs(self.libvirt.images_dir)
            return st.f_bavail * st.f_frsize
        except OSError:
            return None

    # --- creating -------------------------------------------------------------------------
    @staticmethod
    def _int(raw, field, low, high, what):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ApiError(422, f"{what} must be a whole number from {low} to {high}.", field)
        return value

    async def validate(self, raw):
        if not isinstance(raw, dict):
            raise ApiError(422, "The VM settings are missing.")
        try:
            name = V.vm_name(raw.get("name"), "name")
        except V.ValidationError as exc:
            raise ApiError(422, exc.message, exc.field) from None
        domains = await self._lv("domains")
        if any(d["name"] == name for d in domains):
            raise ApiError(422, f"A virtual machine named '{name}' already exists.", "name")
        preset = raw.get("preset")
        if preset not in vm_xml.PRESETS:
            raise ApiError(422, "Choose Linux or Windows.", "preset")
        host = await self._lv("host")
        vcpus = self._int(raw, "vcpus", 1, host["threads"], "vCPUs")
        memory = self._int(raw, "memory_mib", MIN_MEMORY_MIB, host["memory"] // MIB, "Memory (MiB)")
        disk = self._int(raw, "disk_gib", 1, MAX_DISK_GIB, "Disk size (GiB)")
        firmware = raw.get("firmware", "uefi")
        if firmware not in vm_xml.FIRMWARES:
            raise ApiError(422, "Firmware is UEFI or legacy BIOS.", "firmware")
        if firmware == "uefi" and not host["efi"]:
            raise ApiError(422, "UEFI firmware isn't available (the ovmf package is missing). Choose legacy BIOS "
                                "or reinstall ovmf.", "firmware")
        net = raw.get("network") or {"mode": "nat"}
        if not isinstance(net, dict) or net.get("mode") not in ("nat", "bridge"):
            raise ApiError(422, "The network is NAT or a bridge.", "network")
        if net["mode"] == "bridge":
            try:
                bridge = V.bridge_name(net.get("bridge"), "network.bridge")
            except V.ValidationError as exc:
                raise ApiError(422, exc.message, exc.field) from None
            if bridge not in self.bridges():
                raise ApiError(422, f"There is no network bridge named '{bridge}' on this computer.", "network.bridge")
            net = {"mode": "bridge", "bridge": bridge}
        else:
            nat = await self._lv("nat_network")
            if not nat["exists"]:
                raise ApiError(422, "libvirt's NAT network 'default' is missing. Reinstall "
                                    "libvirt-daemon-config-network, or choose a bridge.", "network")
            net = {"mode": "nat"}
        iso = self.isos.path(raw["iso"], "iso") if raw.get("iso") else None
        drivers = None
        if raw.get("drivers_iso"):
            if preset != "windows":
                raise ApiError(422, "A driver ISO is only for Windows.", "drivers_iso")
            drivers = self.isos.path(raw["drivers_iso"], "drivers_iso")
        return {"name": name, "preset": preset, "vcpus": vcpus, "memory_mib": memory, "disk_gib": disk,
                "firmware": firmware, "network": net, "iso": iso, "drivers_iso": drivers,
                "start": bool(raw.get("start", True)), "autostart": bool(raw.get("autostart", False)),
                "kvm": host["kvm"]}

    async def create(self, raw):
        spec = await self.validate(raw)
        volume, path = await self._lv("create_volume", spec["name"], spec["disk_gib"] * GIB)
        xml = vm_xml.build_domain({
            **spec, "uuid": str(uuid.uuid4()), "pool": "nimbus-images", "volume": volume,
            "vnc_password": "".join(secrets.choice(VNC_ALPHABET) for _ in range(8)),
            "created": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        }, kvm=spec["kvm"])
        try:
            await self._lv("define", xml)
        except ApiError:
            await self._lv("delete_volume", volume)          # don't leave an orphan disk behind
            raise
        if spec["autostart"]:
            await self._lv("set_autostart", spec["name"], True)
        message = f"'{spec['name']}' was created, with its disk at {path}."
        started = False
        if spec["start"]:
            try:
                await self._lv("start", spec["name"])
                started = True
                message = f"'{spec['name']}' was created and started."
            except ApiError as exc:
                message = f"'{spec['name']}' was created, but it didn't start: {exc.message}"
        return {"name": spec["name"], "disk": path, "started": started, "message": message}

    # --- lifecycle --------------------------------------------------------------------------
    async def _domain(self, name):
        try:
            V.vm_name(name)
        except V.ValidationError:
            raise ApiError(404, f"There is no virtual machine named '{name}'.", "name") from None
        for d in await self._lv("domains"):
            if d["name"] == name:
                return d
        raise ApiError(404, f"There is no virtual machine named '{name}'.", "name")

    async def action(self, name, action):
        d = await self._domain(name)
        state = d["state"]
        if action == "start":
            if state != "stopped" and state != "crashed":
                raise ApiError(409, f"'{name}' is already {state}.")
            await self._lv("start", name)
            return f"'{name}' started."
        if action == "resume":
            if state != "paused":
                raise ApiError(409, f"'{name}' isn't paused.")
            await self._lv("resume", name)
            return f"'{name}' continues."
        if state not in ("running", "paused", "stopping"):
            raise ApiError(409, f"'{name}' isn't running.")
        if action == "shutdown":
            await self._lv("shutdown", name)
            return f"'{name}' was asked to shut down. If its system doesn't react, use Force off."
        if action == "force-off":
            await self._lv("force_off", name)
            return f"'{name}' was turned off."
        if action == "reboot":
            await self._lv("reboot", name)
            return f"'{name}' was asked to restart."
        raise ApiError(404, f"Unknown action '{action}'.")

    async def set_autostart(self, name, enabled):
        await self._domain(name)
        await self._lv("set_autostart", name, bool(enabled))
        return f"'{name}' {'starts' if enabled else 'no longer starts'} when the computer starts."

    async def change_media(self, name, iso):
        d = await self._domain(name)
        if not d["cdroms"]:
            raise ApiError(409, f"'{name}' has no CD-ROM drive.")
        drive = d["cdroms"][0]
        path = self.isos.path(iso, "iso") if iso else None
        await self._lv("change_cdrom", name, drive["dev"], drive["bus"] or "sata", path)
        return f"'{iso}' is in {name}'s CD drive." if iso else f"The CD was ejected from '{name}'."

    async def delete(self, name, delete_disks):
        d = await self._domain(name)
        await self._lv("undefine", name)
        self._remove_token(d["uuid"])
        kept, deleted = [], []
        for disk in d["disks"]:
            if not disk.get("path"):
                continue
            if delete_disks and disk.get("in_pool"):
                await self._lv("delete_volume", os.path.basename(disk["path"]))
                deleted.append(disk["path"])
            else:
                kept.append(disk["path"])
        message = f"'{name}' was deleted."
        if deleted:
            message += f" Its disk was deleted ({', '.join(deleted)})."
        if kept:
            reason = "" if not delete_disks else " (only disks in the NimbusOS disk folder are deleted)"
            message += f" Kept: {', '.join(kept)}{reason}."
        return {"message": message, "deleted": deleted, "kept": kept}

    # --- consoles ---------------------------------------------------------------------------
    def _tokens_fd(self):
        return os.open(self.tokens_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    async def console(self, name):
        d = await self._domain(name)
        target = await self._lv("vnc_target", name)
        if target is None:
            if d["state"] not in ("running", "paused"):
                raise ApiError(409, f"'{name}' isn't running. Start it to see its screen.")
            raise ApiError(409, f"'{name}' has no VNC display, so it has no console here.")
        host, port, password = target
        token = secrets.token_urlsafe(24)
        fd = self._tokens_fd()
        try:
            tmp = f".tmp-{d['uuid']}"
            f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=fd)
            try:
                os.write(f, f"{token}: {host}:{port}\n".encode())
            finally:
                os.close(f)
            os.replace(tmp, d["uuid"], src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
        return {"path": f"/websockify?token={token}", "password": password or ""}

    def _remove_token(self, uid):
        try:
            fd = self._tokens_fd()
        except OSError:
            return
        try:
            os.unlink(uid, dir_fd=fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)

    def _prune_tokens(self, running):
        try:
            fd = self._tokens_fd()
        except OSError:
            return
        try:
            for entry in os.listdir(fd):
                if entry not in running:
                    try:
                        os.unlink(entry, dir_fd=fd)
                    except OSError:
                        pass
        finally:
            os.close(fd)

    def clear_tokens(self):
        self._prune_tokens(set())

    # --- ISO library ------------------------------------------------------------------------
    async def iso_list(self):
        users = {}
        try:
            for d in await asyncio.to_thread(self.libvirt.domains):
                for c in d["cdroms"]:
                    if c.get("path") and os.path.dirname(c["path"]) == self.isos.root:
                        users.setdefault(os.path.basename(c["path"]), []).append(d["name"])
        except LibvirtUnavailable:
            pass
        return {"isos": [{**i, "used_by": users.get(i["name"], [])} for i in self.isos.list()],
                "path": self.isos.root, "max_bytes": self.isos.max_bytes, "free_bytes": self.isos.free_bytes()}

    async def iso_delete(self, name):
        listing = await self.iso_list()
        entry = next((i for i in listing["isos"] if i["name"] == name), None)
        if entry and entry["used_by"]:
            raise ApiError(409, f"'{name}' is in the CD drive of {', '.join(entry['used_by'])}. Eject it there first.")
        self.isos.delete(name)
        return f"'{name}' was deleted."


# --- routes ----------------------------------------------------------------------------------------
router = APIRouter()


def svc(request) -> VmService:
    return request.app.state.vms


async def json_body(request):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(422, "The request is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(422, "The request must be a JSON object.")
    return body


@router.get("/api/vms")
async def list_vms(request: Request):
    return await svc(request).list()


@router.get("/api/vms/options")
async def options(request: Request):
    return await svc(request).options()


@router.post("/api/vms")
async def create(request: Request):
    return await svc(request).create(await json_body(request))


@router.post("/api/vms/{name}/actions/{action}")
async def action(name: str, action: str, request: Request):
    if action not in ("start", "shutdown", "force-off", "reboot", "resume"):
        raise ApiError(404, f"Unknown action '{action}'.")
    return {"message": await svc(request).action(name, action)}


@router.put("/api/vms/{name}/autostart")
async def autostart(name: str, request: Request):
    body = await json_body(request)
    if not isinstance(body.get("enabled"), bool):
        raise ApiError(422, "Say whether the VM starts with the computer.", "enabled")
    return {"message": await svc(request).set_autostart(name, body["enabled"])}


@router.put("/api/vms/{name}/media")
async def media(name: str, request: Request):
    iso = (await json_body(request)).get("iso")
    return {"message": await svc(request).change_media(name, iso or None)}


@router.delete("/api/vms/{name}")
async def delete(name: str, request: Request, delete_disks: bool = Query(False)):
    return await svc(request).delete(name, delete_disks)


@router.post("/api/vms/{name}/console")
async def console(name: str, request: Request):
    result = await svc(request).console(name)
    return {**result, "port": request.app.state.settings.websockify_port}


@router.get("/api/isos")
async def isos(request: Request):
    return await svc(request).iso_list()


@router.delete("/api/isos/{name}")
async def iso_delete(name: str, request: Request):
    return {"message": await svc(request).iso_delete(name)}


@router.post("/api/isos/uploads")
async def upload_begin(request: Request):
    body = await json_body(request)
    return svc(request).isos.begin(body.get("name"), body.get("size"))


@router.put("/api/isos/uploads/{uid}")
async def upload_chunk(uid: str, request: Request, offset: int = Query(..., ge=0)):
    return await svc(request).isos.write(uid, offset, request.stream())


@router.post("/api/isos/uploads/{uid}/finish")
async def upload_finish(uid: str, request: Request):
    result = svc(request).isos.finish(uid)
    return {**result, "message": f"'{result['name']}' is in the ISO library."}


@router.delete("/api/isos/uploads/{uid}")
async def upload_cancel(uid: str, request: Request):
    svc(request).isos.cancel(uid)
    return {"message": "The upload was cancelled."}
