# =============================================================================
# nimbus_control/shares_service.py: shares, SMB users and Settings
# =============================================================================
# What: - shares: GET /api/shares lists each share with its folder, whether
#         its disk is there, its usage (du, refreshed in the background every
#         10 minutes) and free space, and the connection strings
#         \\<host>\<share> and smb://<host>.local/<share>, plus the SMB users.
#         Create, edit and delete are jobs: the new list goes to privd's
#         share.apply (rendered, checked with testparm, swapped in, reloaded,
#         rolled back on failure) and is saved to shares.json only when that
#         worked. Deleting removes the export; the folder goes only with
#         ?delete_data=true (privd share.delete_data);
#       - SMB users: add, new password, delete (also from every share's
#         permissions), and "Test connection" (smbclient through privd);
#         passwords go straight to privd and are never stored or logged;
#       - Settings: Samba (on or off, workgroup, description, NetBIOS) and
#         the disk temperature alert thresholds, in settings.json; Samba
#         changes are applied through share.apply and samba.settings.
# Why:  spec Tab C (Shares, SMB users) and Settings; docs/PLAN.md NOTE-68.
# =============================================================================

import asyncio
import os
import socket
import time

from fastapi import APIRouter, Query, Request

from nimbus_common import shares as S
from nimbus_common.validate import ValidationError, smb_user

from .errors import ApiError
from .jobs import JobFailed
from .privd_client import PrivdError

USAGE_SECONDS = 600


async def privd_job(privd, job, verb, args, timeout=3600):
    """Run a streaming privd verb inside a job: its lines go to the job's log."""
    result = None
    try:
        async for kind, value in privd.stream(verb, args, timeout=timeout):
            if kind == "log":
                job.log(value)
            else:
                result = value
    except PrivdError as exc:
        raise JobFailed(exc.message) from None
    return result


def connect_strings(name):
    host = socket.gethostname()
    return {"windows": f"\\\\{host}\\{name}", "mac": f"smb://{host}.local/{name}"}


class Usage:
    """Folder sizes by du, measured in the background and kept 10 minutes."""

    def __init__(self):
        self.cache = {}             # path -> (time, bytes, complete)
        self.running = set()

    def get(self, path):
        entry = self.cache.get(path)
        if (entry is None or time.monotonic() - entry[0] > USAGE_SECONDS) and path not in self.running:
            self.running.add(path)
            asyncio.get_running_loop().create_task(self._measure(path))
        return None if entry is None else {"used": entry[1], "complete": entry[2]}

    async def _measure(self, path):
        try:
            proc = await asyncio.create_subprocess_exec("/usr/bin/du", "-s", "-x", "--block-size=1", path,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            used = int(out.split()[0]) if out.split() else 0
            self.cache[path] = (time.monotonic(), used, proc.returncode == 0)
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            self.running.discard(path)


class SharesService:
    def __init__(self, state, privd, jobs):
        self.state = state
        self.privd = privd
        self.jobs = jobs
        self.usage = Usage()

    # --- reading -----------------------------------------------------------------------------
    def specs(self):
        out = []
        for raw in self.state.shares():
            try:
                out.append(S.share_spec(raw, ""))
            except ValidationError:
                continue
        return out

    async def users(self):
        return (await self.privd.call("smbuser.list"))["users"]

    async def list(self):
        try:
            users = await self.users()
            users_error = None
        except PrivdError as exc:
            users, users_error = [], exc.message
        shares = []
        for spec in self.specs():
            path = S.share_path(spec)
            root, available = S.location_root(spec["location"])
            space = None
            if available and os.path.isdir(path):
                st = os.statvfs(path)
                space = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
            shares.append({**spec, "path": path, "available": available, "space": space,
                           "usage": self.usage.get(path) if available else None, "connect": connect_strings(spec["name"])})
        return {"shares": shares, "users": users, "users_error": users_error, "host": socket.gethostname(),
                "settings": self.state.settings()["samba"]}

    # --- changing shares ------------------------------------------------------------------------
    async def _check(self, spec, users):
        known = {u["name"] for u in users}
        unknown = sorted(set(spec["users"]) - known)
        if unknown:
            raise ApiError(422, f"'{unknown[0]}' is not an SMB user. Add the user first.", "users")
        root, available = S.location_root(spec["location"])
        if not available:
            where = "The pool" if spec["location"] == "pool" else f"The disk {spec['location'][5:]}"
            raise ApiError(422, f"{where} isn't mounted ({root}). Choose another location.", "location")

    def apply_job(self, kind, shares, message, after=None):
        settings = self.state.settings()["samba"]

        async def work(job):
            await privd_job(self.privd, job, "share.apply", {"shares": shares, "settings": settings})
            async with self.state.lock:
                self.state.save_shares([{**s, "path": S.share_path(s)} for s in shares])
            if after:
                await after(job)
        return self.jobs.start(kind, "shares", work, message)

    async def create(self, raw):
        spec = S.share_spec(raw, "")
        current = self.specs()
        if any(s["name"].lower() == spec["name"].lower() for s in current):
            raise ApiError(422, f"A share named '{spec['name']}' already exists.", "name")
        await self._check(spec, await self.users())
        return self.apply_job("create share", [*current, spec], f"Share '{spec['name']}' created.")

    async def update(self, name, raw):
        current = self.specs()
        old = next((s for s in current if s["name"] == name), None)
        if old is None:
            raise ApiError(404, f"There is no share named '{name}'.")
        spec = S.share_spec({**raw, "name": name}, "")
        if spec["location"] != old["location"]:
            raise ApiError(422, "A share's location can't change: its files would stay behind. Create a new share "
                                "and move the files instead.", "location")
        await self._check(spec, await self.users())
        return self.apply_job("edit share", [spec if s["name"] == name else s for s in current],
                              f"Share '{name}' saved.")

    async def delete(self, name, delete_data):
        current = self.specs()
        old = next((s for s in current if s["name"] == name), None)
        if old is None:
            raise ApiError(404, f"There is no share named '{name}'.")
        rest = [s for s in current if s["name"] != name]

        async def remove_data(job):
            if delete_data:
                await privd_job(self.privd, job, "share.delete_data", {"name": name, "location": old["location"]})

        kept = "" if delete_data else f" Its files are still in {S.share_path(old)}."
        return self.apply_job("delete share", rest, f"Share '{name}' deleted.{kept}", after=remove_data)

    # --- SMB users ---------------------------------------------------------------------------------
    async def add_user(self, name, password):
        smb_user(name, "name")
        S.password(password)
        try:
            await self.privd.call("smbuser.add", {"user": name, "password": password})
        except PrivdError as exc:
            raise ApiError(422 if exc.code == "invalid_argument" else 502, exc.message,
                           "name" if exc.field == "user" else exc.field) from None
        return f"SMB user '{name}' added."

    async def set_password(self, name, password):
        S.password(password)
        await self.privd.call("smbuser.passwd", {"user": smb_user(name, "name"), "password": password})
        return f"The password of '{name}' was changed."

    async def delete_user(self, name):
        smb_user(name, "name")
        current = self.specs()
        if any(name in s["users"] for s in current):
            shares = [{**s, "users": {u: a for u, a in s["users"].items() if u != name}} for s in current]
            await self.privd.call("share.apply", {"shares": shares, "settings": self.state.settings()["samba"]}, timeout=600)
            async with self.state.lock:
                self.state.save_shares([{**s, "path": S.share_path(s)} for s in shares])
        await self.privd.call("smbuser.delete", {"user": name})
        return f"SMB user '{name}' was removed. Files they made keep their owner."

    async def test(self, name, password):
        smb_user(name, "user")
        S.password(password)
        return await self.privd.call("samba.test", {"user": name, "password": password})

    # --- settings ----------------------------------------------------------------------------------
    async def save_settings(self, raw):
        current = self.state.settings()
        samba = S.samba_settings({**current["samba"], **(raw.get("samba") or {})}, "samba")
        alerts = {**current["alerts"], **(raw.get("alerts") or {})}
        for key, low, high in (("hdd_temp_c", 30, 90), ("nvme_temp_c", 40, 100)):
            value = alerts.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ApiError(422, f"Must be a whole number from {low} to {high} °C.", f"alerts.{key}")
        if samba != current["samba"]:
            await self.privd.call("share.apply", {"shares": self.specs(), "settings": samba}, timeout=600)
            await self.privd.call("samba.settings", {"enabled": samba["enabled"], "netbios": samba["netbios"]}, timeout=180)
        async with self.state.lock:
            self.state.save_settings({"samba": samba, "alerts": {k: alerts[k] for k in ("hdd_temp_c", "nvme_temp_c")}})
        return self.state.settings()


# --- routes ----------------------------------------------------------------------------------------
router = APIRouter()


def svc(request) -> SharesService:
    return request.app.state.shares


async def body(request):
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(422, "The request is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(422, "The request must be a JSON object.")
    return data


@router.get("/api/shares")
async def list_shares(request: Request):
    return await svc(request).list()


@router.post("/api/shares")
async def create_share(request: Request):
    job = await svc(request).create(await body(request))
    return {"job": job.id}


@router.put("/api/shares/{name}")
async def update_share(name: str, request: Request):
    job = await svc(request).update(name, await body(request))
    return {"job": job.id}


@router.delete("/api/shares/{name}")
async def delete_share(name: str, request: Request, delete_data: bool = Query(False)):
    job = await svc(request).delete(name, delete_data)
    return {"job": job.id}


@router.post("/api/smbusers")
async def add_user(request: Request):
    data = await body(request)
    return {"message": await svc(request).add_user(data.get("name"), data.get("password"))}


@router.put("/api/smbusers/{name}/password")
async def set_password(name: str, request: Request):
    return {"message": await svc(request).set_password(name, (await body(request)).get("password"))}


@router.delete("/api/smbusers/{name}")
async def delete_user(name: str, request: Request):
    return {"message": await svc(request).delete_user(name)}


@router.post("/api/smbusers/test")
async def test_connection(request: Request):
    data = await body(request)
    return await svc(request).test(data.get("user"), data.get("password"))


@router.get("/api/settings")
async def get_settings(request: Request):
    return request.app.state.store.settings()


@router.put("/api/settings")
async def put_settings(request: Request):
    return await svc(request).save_settings(await body(request))
