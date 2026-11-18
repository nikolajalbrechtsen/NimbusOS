# =============================================================================
# nimbus_control/updates_service.py: the Updates page's API
# =============================================================================
# What: GET  /api/updates            status (nimbus-update via privd): version,
#                                    channel, policy, last check, pending
#                                    updates by group, release notes, reboot
#                                    state, errors with their fix
#       POST /api/updates/check      "Check now"; returns the new status
#       PUT  /api/updates/channel    {"channel": "stable"|"beta"}
#       PUT  /api/updates/policy     {"policy": "automatic"|"notify"|"off"}
#       GET  /api/updates/history    past apt runs (?limit=1..500)
#       POST /api/updates/install    starts the install job; returns its id
#       GET  /api/updates/job        the running job, for a reload/reconnect
#       WS   /ws/updates/{job}       the job's log, line by line, then
#                                    {"type": "done", "ok", "exit_status"}
#       The pending count for the sidebar badge comes from a status cached
#       for 60 seconds.
# Why:  spec section 4.4; all update logic stays in nimbus-update.
# =============================================================================

import time

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from nimbus_common.validate import ValidationError, job_id

from .auth import WS_PROTOCOL
from .errors import ApiError
from .privd_client import PrivdError

CACHE_SECONDS = 60


class UpdatesService:
    def __init__(self, privd):
        self.privd = privd
        self._status = None
        self._status_at = 0.0
        self.job = None

    def _remember(self, status):
        if isinstance(status, dict):
            self._status, self._status_at = status, time.monotonic()
        return status

    async def status(self):
        return self._remember(await self.privd.call("update.status"))

    async def pending_count(self):
        if self._status is None or time.monotonic() - self._status_at > CACHE_SECONDS:
            await self.status()
        counts = (self._status or {}).get("counts") or {}
        return sum(int(v) for v in counts.values())

    def invalidate(self):
        self._status = None


class ChannelBody(BaseModel):
    channel: str


class PolicyBody(BaseModel):
    policy: str


router = APIRouter()


def service(request) -> UpdatesService:
    return request.app.state.updates


@router.get("/api/updates")
async def get_status(request: Request):
    return await service(request).status()


@router.post("/api/updates/check")
async def check(request: Request):
    svc = service(request)
    return svc._remember(await svc.privd.call("update.check", timeout=960))


@router.put("/api/updates/channel")
async def set_channel(body: ChannelBody, request: Request):
    if body.channel not in ("stable", "beta"):
        raise ApiError(422, "The channel is either stable or beta.", "channel")
    svc = service(request)
    await svc.privd.call("update.set_channel", {"channel": body.channel}, timeout=320)
    return await svc.status()


@router.put("/api/updates/policy")
async def set_policy(body: PolicyBody, request: Request):
    if body.policy not in ("automatic", "notify", "off"):
        raise ApiError(422, "The policy is automatic, notify or off.", "policy")
    svc = service(request)
    await svc.privd.call("update.set_policy", {"policy": body.policy})
    return await svc.status()


@router.get("/api/updates/history")
async def history(request: Request, limit: int = Query(50, ge=1, le=500)):
    return await service(request).privd.call("update.history", {"limit": limit})


@router.post("/api/updates/install")
async def install(request: Request):
    svc = service(request)
    result = await svc.privd.call("update.install")
    svc.job = result["job"]
    return {"job": svc.job}


@router.get("/api/updates/job")
async def current_job(request: Request):
    return {"job": service(request).job}


@router.websocket("/ws/updates/{job}")
async def follow(websocket: WebSocket, job: str):
    svc = websocket.app.state.updates
    try:
        job_id(job)
    except ValidationError:
        await websocket.close(code=4404)
        return
    await websocket.accept(subprotocol=WS_PROTOCOL)
    try:
        async for kind, value in svc.privd.stream("update.follow", {"job": job}, timeout=7200):
            if kind == "log":
                await websocket.send_json({"type": "log", "line": value})
            else:
                await websocket.send_json({"type": "done", **(value or {})})
                svc.invalidate()
                if svc.job == job:
                    svc.job = None
        await websocket.close()
    except PrivdError as exc:
        await websocket.send_json({"type": "error", "message": exc.message})
        await websocket.close()
    except WebSocketDisconnect:
        pass
