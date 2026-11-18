# =============================================================================
# nimbus_control/storage_service.py: disks, the pool, parity, the NAS wizard
# =============================================================================
# What: - GET /api/storage: every disk (privd disk.list: role, SMART, the
#         system disk and live medium marked), the managed fstab entries
#         (a set-up disk that is missing shows up), the pool (mode derived
#         from fstab and the SnapRAID configuration, its disks, usage),
#         parity status, the shares on each location, and whether this is
#         the live session (then nothing can be changed);
#       - jobs, each streaming privd's log: format (the serial typed to
#         confirm), add an existing filesystem, unmount (refused while a
#         share lives on the disk), pool changes (refused when they would
#         take the pool away from its shares; shares are re-applied after),
#         and the "Set up NAS" wizard:
#           format the chosen disks (XFS, disk1…, parity1…) → pool mode →
#           the first SMB user → the default shares (media and isos Secure,
#           documents, backups and timemachine Private) → Samba on;
#       - SMART refresh and "Sync now" for parity.
# Why:  spec Tab C (Disks, Pool, first-run wizard); docs/PLAN.md §4.5,
#       NOTE-26, NOTE-68.
# =============================================================================

import socket

from fastapi import APIRouter, Request

from nimbus_common import shares as S
from nimbus_common.validate import disk_label, smb_user

from .errors import ApiError
from .jobs import JobFailed
from .shares_service import body, privd_job

FILESYSTEMS = ("xfs", "ext4", "btrfs")
DEFAULT_SHARES = {
    "media": {"security": "secure", "recycle": {"enabled": True, "days": 30}},
    "documents": {"security": "private", "recycle": {"enabled": True, "days": 30}},
    "backups": {"security": "private", "recycle": {"enabled": False, "days": 30}},
    "isos": {"security": "secure", "recycle": {"enabled": False, "days": 30}},
    "timemachine": {"security": "private", "timemachine": {"enabled": True, "max_gb": 0}},
}


def derive_pool(listing):
    entries = listing["fstab"]
    pool = next((e for e in entries if e["kind"] == "pool"), None)
    parity_on = listing["pool"].get("parity_configured")
    mode = "single" if pool is None else "parity" if parity_on else "pooled"
    return {"mode": mode, "policy": pool["policy"] if pool else "mfs", "data": pool["branches"] if pool else [],
            "parity": [e["label"] for e in entries if e["kind"] == "disk" and e["role"] == "parity"] if parity_on else [],
            **listing["pool"]}


class StorageService:
    def __init__(self, state, privd, jobs, shares):
        self.state = state
        self.privd = privd
        self.jobs = jobs
        self.shares = shares

    async def listing(self):
        return await self.privd.call("disk.list", timeout=300)

    async def overview(self):
        listing = await self.listing()
        pool = derive_pool(listing)
        parity = None
        if pool["mode"] == "parity":
            parity = await self.privd.call("snapraid.status", timeout=700)
        by_location = {}
        for spec in self.shares.specs():
            by_location.setdefault(spec["location"], []).append(spec["name"])
        return {"live": listing["live"], "disks": listing["disks"], "fstab": listing["fstab"], "pool": pool,
                "parity": parity, "shares_by_location": by_location,
                "missing": [e["label"] for e in listing["fstab"] if e["kind"] == "disk" and not e.get("present")]}

    async def _refuse_live(self):
        if (await self.listing())["live"]:
            raise ApiError(409, "Install NimbusOS to set up storage. Disks can't be changed in the live session.")

    # --- disks ----------------------------------------------------------------------------------
    async def format(self, raw):
        await self._refuse_live()
        args = {"device": raw.get("device"), "serial": str(raw.get("serial") or "").strip(),
                "fs": raw.get("fs", "xfs"), "label": disk_label(raw.get("label"), "label"),
                "role": raw.get("role", "data")}
        if args["fs"] not in FILESYSTEMS:
            raise ApiError(422, "The filesystem is XFS, ext4 or btrfs.", "fs")
        if args["role"] not in ("data", "parity"):
            raise ApiError(422, "A disk is a data or a parity disk.", "role")

        async def work(job):
            await privd_job(self.privd, job, "disk.format", args)
        return self.jobs.start("format", "storage", work, f"{args['device']} is ready as '{args['label']}'.")

    async def mount(self, raw):
        await self._refuse_live()
        args = {"partition": raw.get("partition"), "label": disk_label(raw.get("label"), "label"),
                "role": raw.get("role", "data")}

        async def work(job):
            await privd_job(self.privd, job, "disk.mount", args)
        return self.jobs.start("mount", "storage", work, f"'{args['label']}' was added.")

    async def unmount(self, label):
        disk_label(label, "label")
        users = [s["name"] for s in self.shares.specs() if s["location"] == f"disk:{label}"]
        if users:
            raise ApiError(409, f"The shares {', '.join(users)} live on '{label}'. Delete or move them first.")

        async def work(job):
            await privd_job(self.privd, job, "disk.unmount", {"label": label})
        return self.jobs.start("unmount", "storage", work, f"'{label}' was unmounted; its data is untouched.")

    async def smart(self, device):
        return await self.privd.call("disk.smart", {"device": device}, timeout=120)

    # --- the pool -------------------------------------------------------------------------------
    async def set_pool(self, raw):
        await self._refuse_live()
        mode = raw.get("mode")
        if mode not in ("single", "pooled", "parity"):
            raise ApiError(422, "The mode is Single, Pooled or Pooled + parity.", "mode")
        args = {"mode": mode, "policy": raw.get("policy", "mfs"), "data": raw.get("data") or [],
                "parity": raw.get("parity") or []}
        on_pool = [s["name"] for s in self.shares.specs() if s["location"] == "pool"]
        if mode == "single" and on_pool:
            raise ApiError(409, f"The shares {', '.join(on_pool)} live on the pool. Delete them (or move their files) "
                                "before turning the pool off.", "mode")
        if mode != "single" and not args["data"]:
            raise ApiError(422, "Choose at least one data disk for the pool.", "data")

        async def work(job):
            await privd_job(self.privd, job, "pool.apply", args)
            specs = self.shares.specs()
            if specs:
                job.log("Applying the shares again")
                await privd_job(self.privd, job, "share.apply", {"shares": specs, "settings": self.state.settings()["samba"]})
        names = {"single": "Single disk", "pooled": "Pooled", "parity": "Pooled + parity"}
        return self.jobs.start("pool", "storage", work, f"Storage mode: {names[mode]}.")

    async def sync_now(self):
        await self.privd.call("snapraid.sync_now")
        return "Parity sync started. It runs in the background; the card shows when it has finished."

    # --- the wizard -------------------------------------------------------------------------------
    async def wizard(self, raw):
        listing = await self.listing()
        if listing["live"]:
            raise ApiError(409, "Install NimbusOS to set up storage. Disks can't be changed in the live session.")
        if self.shares.specs():
            raise ApiError(409, "Shares exist already; change them on the Shares tab instead.")
        mode = raw.get("mode", "single")
        if mode not in ("single", "pooled", "parity"):
            raise ApiError(422, "The mode is Single, Pooled or Pooled + parity.", "mode")
        disks = {d["path"]: d for d in listing["disks"]}
        taken = {e["label"] for e in listing["fstab"] if e["kind"] == "disk"}
        new = []
        for i, item in enumerate(raw.get("disks") or []):
            dev = disks.get(item.get("device")) if isinstance(item, dict) else None
            if dev is None or dev["role"] != "unassigned" or dev["system"]:
                raise ApiError(422, "Choose disks that are not in use.", f"disks.{i}")
            role = item.get("role", "data")
            if role not in ("data", "parity") or (role == "parity" and mode != "parity"):
                raise ApiError(422, "Parity disks are only for Pooled + parity.", f"disks.{i}")
            if str(item.get("serial") or "").strip() != (dev["serial"] or dev["name"]):
                raise ApiError(422, f"Type the {'serial number' if dev['serial'] else 'name'} of {dev['path']} "
                                    f"({dev['serial'] or dev['name']}) to confirm erasing it.", f"disks.{i}.serial")
            prefix = "disk" if role == "data" else "parity"
            n = 1
            while f"{prefix}{n}" in taken:
                n += 1
            taken.add(f"{prefix}{n}")
            new.append({"device": dev["path"], "serial": item["serial"].strip(), "fs": "xfs", "label": f"{prefix}{n}",
                        "role": role})
        existing = [disk_label(x, "existing") for x in raw.get("existing") or []]
        ready = {d["label"] for d in listing["disks"] if d["role"] == "data" and d["label"]}
        if any(x not in ready for x in existing):
            raise ApiError(422, "Only disks that are set up as data disks can be used as they are.", "existing")
        data = existing + [d["label"] for d in new if d["role"] == "data"]
        parity = [d["label"] for d in new if d["role"] == "parity"]
        use_system = bool(raw.get("system"))
        if mode == "single" and not use_system and len(data) != 1:
            raise ApiError(422, "Single disk mode uses one data disk, or the system disk.", "disks")
        if mode != "single" and not data:
            raise ApiError(422, "A pool needs at least one data disk.", "disks")
        if mode == "parity" and not parity:
            raise ApiError(422, "Pooled + parity needs a parity disk at least as large as the largest data disk.", "disks")
        names = raw.get("shares") or []
        if not isinstance(names, list) or any(n not in DEFAULT_SHARES for n in names):
            raise ApiError(422, "Choose shares from the list.", "shares")
        user = raw.get("user") or {}
        smb_user(user.get("name"), "user.name")
        S.password(user.get("password"), "user.password")
        location = "pool" if mode != "single" else ("system" if use_system else f"disk:{data[0]}")

        async def work(job):
            for d in new:
                job.log(f"── Preparing {d['device']} as {d['label']} ({d['role']})")
                await privd_job(self.privd, job, "disk.format", d)
            if mode != "single":
                job.log("── Setting up the pool")
                await privd_job(self.privd, job, "pool.apply", {"mode": mode, "policy": "mfs", "data": data, "parity": parity})
            job.log(f"── Adding the SMB user {user['name']}")
            users = {u["name"] for u in (await self.privd.call("smbuser.list"))["users"]}
            if user["name"] in users:
                await self.privd.call("smbuser.passwd", {"user": user["name"], "password": user["password"]})
            else:
                await self.privd.call("smbuser.add", {"user": user["name"], "password": user["password"]})
            job.log(f"── Creating the shares: {', '.join(names) or 'none'}")
            specs = [S.share_spec({"name": n, "location": location, "users": {user["name"]: "rw"}, **DEFAULT_SHARES[n]}, "")
                     for n in names]
            settings = {**self.state.settings()["samba"], "enabled": True}
            await privd_job(self.privd, job, "share.apply", {"shares": specs, "settings": settings})
            async with self.state.lock:
                self.state.save_shares([{**s, "path": S.share_path(s)} for s in specs])
                self.state.save_settings({**self.state.settings(), "samba": settings})
            try:
                await self.privd.call("samba.settings", {"enabled": True, "netbios": settings["netbios"]}, timeout=180)
            except Exception as exc:                 # shares exist; say what's left to do
                raise JobFailed(f"The shares are set up, but Samba could not be started: {exc}") from None
            host = socket.gethostname()
            job.log(f"Done. Windows: \\\\{host}\\{names[0] if names else ''}   macOS/Linux: smb://{host}.local/")
        return self.jobs.start("wizard", "storage", work, "Your NAS is ready.")


# --- routes ------------------------------------------------------------------------------------------
router = APIRouter()


def svc(request) -> StorageService:
    return request.app.state.storage


@router.get("/api/storage")
async def overview(request: Request):
    return await svc(request).overview()


@router.post("/api/storage/disks/format")
async def format_disk(request: Request):
    return {"job": (await svc(request).format(await body(request))).id}


@router.post("/api/storage/disks/mount")
async def mount_disk(request: Request):
    return {"job": (await svc(request).mount(await body(request))).id}


@router.post("/api/storage/disks/{label}/unmount")
async def unmount_disk(label: str, request: Request):
    return {"job": (await svc(request).unmount(label)).id}


@router.post("/api/storage/disks/smart")
async def smart(request: Request):
    return await svc(request).smart((await body(request)).get("device"))


@router.put("/api/storage/pool")
async def set_pool(request: Request):
    return {"job": (await svc(request).set_pool(await body(request))).id}


@router.post("/api/storage/parity/sync")
async def sync_now(request: Request):
    return {"message": await svc(request).sync_now()}


@router.post("/api/storage/wizard")
async def wizard(request: Request):
    return {"job": (await svc(request).wizard(await body(request))).id}

