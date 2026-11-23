# =============================================================================
# nimbus_control/jobs.py: long-running operations with a live log
# =============================================================================
# What: JobManager.start(kind, key, work) runs the coroutine work(job) in the
#       background and returns the job at once. Inside, job.log(line) adds a
#       line and job.run(argv) runs a program (argument list, never a shell)
#       and streams its output line by line. One job per key (an app's name)
#       at a time; a second one gets ApiError 409. stream(job) yields the
#       lines so far, then new ones live, then the end; a window that
#       reconnects sees the whole log again. Finished jobs are kept for an
#       hour.
#       Routes: GET /api/jobs/{id} (state) and WS /ws/jobs/{id} (the log,
#       then {"type": "done", "ok", "message"}).
# Why:  spec Tab A: "Stream pull and deploy output to the UI over WebSocket".
# =============================================================================

import asyncio
import os
import secrets
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from .auth import WS_PROTOCOL
from .errors import ApiError
from .privd_client import PrivdError

KEEP_SECONDS = 3600
MAX_LINES = 20000


class JobFailed(Exception):
    """Ends a job with a message for the user (no traceback)."""


class Job:
    def __init__(self, kind, key):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.key = key
        self.lines = []
        self.state = "running"
        self.message = ""
        self.finished_at = None
        self._changed = asyncio.Event()

    def log(self, line):
        if len(self.lines) < MAX_LINES:
            self.lines.append(str(line))
        self._changed.set()

    async def run(self, argv, *, cwd=None, env=None, timeout=3600):
        """Run ARGV, streaming its output; raise JobFailed if it fails."""
        if not argv or not argv[0].startswith("/"):
            raise ValueError("commands must start with an absolute path")
        self.log("$ " + " ".join([os.path.basename(argv[0]), *argv[1:]]))
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def pump():
            async for raw in proc.stdout:
                self.log(raw.decode("utf-8", "replace").rstrip("\n"))
        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise JobFailed(f"{os.path.basename(argv[0])} did not finish within {timeout // 60} minutes.") from None
        if proc.returncode != 0:
            raise JobFailed(f"{os.path.basename(argv[0])} failed (exit status {proc.returncode}); the log above says why.")

    def finish(self, ok, message):
        self.state = "ok" if ok else "failed"
        self.message = message
        self.finished_at = time.monotonic()
        self._changed.set()

    def summary(self):
        return {"id": self.id, "kind": self.kind, "key": self.key, "state": self.state, "message": self.message}

    async def follow(self):
        """Yield ("log", line)... then ("done", summary)."""
        sent = 0
        while True:
            while sent < len(self.lines):
                yield "log", self.lines[sent]
                sent += 1
            if self.state != "running":
                yield "done", self.summary()
                return
            self._changed.clear()
            if sent < len(self.lines) or self.state != "running":
                continue
            await self._changed.wait()


class JobManager:
    def __init__(self):
        self.jobs = {}
        self._tasks = set()

    def _prune(self):
        now = time.monotonic()
        for job_id in [j for j, job in self.jobs.items() if job.finished_at and now - job.finished_at > KEEP_SECONDS]:
            del self.jobs[job_id]

    def running(self, key):
        return next((j for j in self.jobs.values() if j.key == key and j.state == "running"), None)

    def start(self, kind, key, work, success_message):
        self._prune()
        busy = self.running(key)
        if busy:
            raise ApiError(409, f"'{key}' is busy ({busy.kind}). Wait until it has finished.")
        job = Job(kind, key)
        self.jobs[job.id] = job

        async def runner():
            try:
                await work(job)
            except JobFailed as exc:
                job.log(str(exc))
                job.finish(False, str(exc))
            except (ApiError, PrivdError) as exc:
                job.log(exc.message)
                job.finish(False, exc.message)
            except Exception as exc:          # a bug: say so plainly, keep the service up
                job.log(f"Internal error: {exc!r}")
                job.finish(False, "Something went wrong inside the Control Center; see the log.")
            else:
                job.finish(True, success_message)

        task = asyncio.get_running_loop().create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)


router = APIRouter()


@router.get("/api/jobs/{job_id}")
async def job_state(job_id: str, request: Request):
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise ApiError(404, "This job is unknown (it may have finished more than an hour ago).")
    return job.summary()


@router.websocket("/ws/jobs/{job_id}")
async def job_log(websocket: WebSocket, job_id: str):
    job = websocket.app.state.jobs.get(job_id)
    if job is None:
        await websocket.close(code=4404)
        return
    await websocket.accept(subprotocol=WS_PROTOCOL)
    try:
        async for kind, value in job.follow():
            if kind == "log":
                await websocket.send_json({"type": "log", "line": value})
            else:
                await websocket.send_json({"type": "done", "ok": value["state"] == "ok", "message": value["message"]})
        await websocket.close()
    except WebSocketDisconnect:
        pass
