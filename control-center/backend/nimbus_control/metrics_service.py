# =============================================================================
# nimbus_control/metrics_service.py: the dashboard's collector
# =============================================================================
# What: MetricsService samples every 2 seconds while someone is looking (a
#       /ws/metrics client, or a GET /api/dashboard less than 10 seconds ago)
#       and stops 10 seconds after the last one. Each sample:
#         host     psutil: total and per-core CPU %, load, memory, swap, the
#                  default-route interface's receive and send rates, the CPU
#                  temperature (hidden when the machine reports none)
#         Docker   one non-blocking stats(stream=1) reader per running
#                  container (docker_stream.py): CPU as a share of the host,
#                  memory = usage − inactive_file, summed per Compose project
#                  (one row per app; an app's companions, the containers
#                  Nextcloud AIO makes itself, count with it, NOTE-29); one /events reader wakes the collector
#                  at once on die, oom, health_status and restart, so an alert
#                  appears within a sample; containers and app states are
#                  listed again every 10 seconds or after an event
#         VMs      one getAllDomainStats() call: CPU from the cpu.time delta
#                  over (interval × host threads), and as % of the VM's own
#                  vCPUs; memory from balloon.rss
#         storage  statvfs of the pool and the system disk; SMART and parity
#                  from the copy refresh_storage() keeps (privd's caches,
#                  fetched in the background once an hour and whenever the
#                  Storage page reads them): the dashboard never starts a scan
#         split    System & desktop = total − Docker − VMs, never below 0, for
#                  CPU and memory separately
#         units    failed nimbus-*, smbd, wsdd2, docker, libvirtd units, every
#                  10 seconds; update status every 5 minutes
#         alerts   alerts.evaluate()
#       One hour of history (1,800 samples) stays in memory; nothing is
#       written to disk. GET /api/dashboard returns everything with the
#       history; WS /ws/metrics pushes each new sample.
# Why:  spec Dashboard (metrics collector, delivery); docs/PLAN.md §4.4,
#       NOTE-69.
# =============================================================================

import asyncio
import collections
import json
import os
import socket
import time

import psutil
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from nimbus_common import shares as S

from . import alerts as alert_rules
from . import docker_stream
from .auth import WS_PROTOCOL
from .docker_api import DockerUnavailable
from .libvirt_api import LibvirtUnavailable
from .privd_client import PrivdError
from .system_service import default_route_iface

SAMPLE_SECONDS = 2.0
HISTORY = 1800
IDLE_STOP = 10.0
CONTAINERS_SECONDS = 10.0
UNITS_SECONDS = 10.0
UPDATES_SECONDS = 300.0
STORAGE_SECONDS = 3600.0
STORAGE_FIRST = 60.0
EVENTS = ("die", "oom", "health_status", "restart", "start", "stop", "destroy", "create")
UNIT_PATTERNS = ("nimbus-*", "smbd.service", "wsdd2.service", "docker.service", "libvirtd.service")
SYSTEMCTL = "/usr/bin/systemctl"
TEMP_SENSORS = (("coretemp", ("Package id 0", "Tdie", "Tctl")), ("k10temp", ("Tctl", "Tdie")),
                ("zenpower", ("Tdie", "Tctl")), ("cpu_thermal", ()), ("acpitz", ()))


class HostSampler:
    """The host's own numbers, from psutil."""

    def __init__(self, ps=psutil, iface=default_route_iface):
        self.ps = ps
        self.iface = iface
        self._net = None
        self._cpu = None
        self._cores = None

    @staticmethod
    def _busy(before, after):
        """CPU % between two cpu_times() readings (idle and iowait count as
        idle; guest time is already part of user time, as psutil counts it)."""
        def total_of(t):
            return sum(t) - getattr(t, "guest", 0) - getattr(t, "guest_nice", 0)
        total = total_of(after) - total_of(before)
        idle = (after.idle + getattr(after, "iowait", 0)) - (before.idle + getattr(before, "iowait", 0))
        return 0.0 if total <= 0 else max(0.0, min(100.0, 100.0 * (total - idle) / total))

    def cpu(self):
        """Own deltas, so other psutil.cpu_percent() callers don't shorten them."""
        total, cores = self.ps.cpu_times(), self.ps.cpu_times(percpu=True)
        cpu = self._busy(self._cpu, total) if self._cpu else 0.0
        if self._cores and len(self._cores) == len(cores):
            per = [self._busy(a, b) for a, b in zip(self._cores, cores)]
        else:
            per = [0.0] * len(cores)
        self._cpu, self._cores = total, cores
        return cpu, per

    def temperature(self):
        try:
            sensors = self.ps.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for chip, labels in TEMP_SENSORS:
            readings = sensors.get(chip) or []
            for label in labels:
                for r in readings:
                    if r.label == label and r.current:
                        return round(r.current, 1)
            if readings and not labels:
                return round(max(r.current for r in readings), 1)
        return None

    def sample(self, now):
        mem = self.ps.virtual_memory()
        swap = self.ps.swap_memory()
        iface = self.iface()
        rx = tx = 0.0
        counters = self.ps.net_io_counters(pernic=True).get(iface) if iface else None
        if counters is not None:
            if self._net and self._net[0] == iface and now > self._net[1]:
                dt = now - self._net[1]
                rx = max(0.0, (counters.bytes_recv - self._net[2]) / dt)
                tx = max(0.0, (counters.bytes_sent - self._net[3]) / dt)
            self._net = (iface, now, counters.bytes_recv, counters.bytes_sent)
        try:
            load = list(os.getloadavg())
        except OSError:
            load = None
        cpu, cores = self.cpu()
        return {"cpu": cpu, "cores": cores, "threads": self.ps.cpu_count() or 1, "load": load,
                "memory": {"total": mem.total, "used": mem.total - mem.available, "available": mem.available},
                "swap": {"total": swap.total, "used": swap.used},
                "net": {"iface": iface, "rx": rx, "tx": tx}, "temperature": self.temperature()}


def split(host, apps, vms):
    """Who uses the CPU and the memory: system & desktop, Docker apps, VMs, free."""
    cpu_docker = sum(a["cpu"] for a in apps.values())
    cpu_vms = sum(v["cpu_host"] for v in vms.values() if v.get("cpu_host") is not None)
    cpu_system = max(0.0, host["cpu"] - cpu_docker - cpu_vms)
    total = host["memory"]["total"]
    mem_docker = sum(a["memory"] for a in apps.values())
    mem_vms = sum(v["memory"] for v in vms.values())
    mem_system = max(0, host["memory"]["used"] - mem_docker - mem_vms)
    return {
        "cpu": {"system": cpu_system, "docker": cpu_docker, "vms": cpu_vms,
                "free": max(0.0, 100.0 - cpu_system - cpu_docker - cpu_vms)},
        "memory": {"total": total, "system": mem_system, "docker": mem_docker, "vms": mem_vms,
                   "free": max(0, total - mem_system - mem_docker - mem_vms)},
    }


def space(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    return {"path": path, "total": total, "used": total - st.f_bfree * st.f_frsize, "free": st.f_bavail * st.f_frsize}


class MetricsService:
    def __init__(self, *, store, privd, docker, libvirt, apps, updates, host=None, docker_socket=docker_stream.SOCKET,
                 clock=time.monotonic, wall=time.time):
        self.store = store
        self.privd = privd
        self.docker = docker
        self.libvirt = libvirt
        self.apps = apps
        self.updates = updates
        self.host = host or HostSampler()
        self.docker_socket = docker_socket
        self.clock = clock
        self.wall = wall
        self.history = {k: collections.deque(maxlen=HISTORY) for k in ("t", "cpu", "memory", "net_rx", "net_tx")}
        self.latest = None
        self.alerts = []
        self.queues = set()
        self.last_request = float("-inf")
        self._task = None
        self._wake = None
        self._lock = asyncio.Lock()     # one sample at a time: deltas need ordered readings
        # Docker
        self.readers = {}               # container id -> task
        self.usage = {}                 # container id -> {cpu, memory}
        self.containers = []
        self.docker_problem = None
        self.app_rows = []
        self.restarts = {}              # container id -> deque[(time, restart count)]
        self._containers_at = float("-inf")
        self._dirty = False
        self._events = None
        # VMs, services, updates, storage
        self.vm_samples = {}            # uuid -> (time, cpu_time)
        self.libvirt_problem = None
        self.units = []
        self._units_at = float("-inf")
        self.update_status = None
        self._updates_at = float("-inf")
        self.storage = None             # {"disks", "fstab", "pool", "parity", "at"}
        self._housekeeping = None

    # --- when to sample ------------------------------------------------------------------------
    def wanted(self):
        return bool(self.queues) or self.clock() - self.last_request < IDLE_STOP

    def touch(self):
        self.last_request = self.clock()
        self._ensure_running()

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        try:
            while self.wanted():
                started = self.clock()
                try:
                    await self.tick()
                except Exception as exc:                   # a bad sample must not end the collector
                    print(f"nimbus-control: metrics sample failed: {exc!r}", flush=True)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), max(0.05, SAMPLE_SECONDS - (self.clock() - started)))
                    await asyncio.sleep(0.3)               # let a burst of events settle
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_docker()

    async def stop(self):
        for task in (self._task, self._housekeeping):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self._stop_docker()

    # --- Docker -------------------------------------------------------------------------------------
    async def _read_stats(self, cid):
        try:
            async for doc in docker_stream.stream(f"/containers/{cid}/stats", self.docker_socket, {"stream": "1"}):
                self.usage[cid] = {"cpu": docker_stream.cpu_share(doc), "memory": docker_stream.memory_used(doc)}
        except (docker_stream.DockerStreamError, OSError, ValueError):
            pass
        finally:
            self.usage.pop(cid, None)
            self.readers.pop(cid, None)

    async def _read_events(self):
        query = {"filters": json.dumps({"type": ["container"], "event": list(EVENTS)})}
        while True:
            try:
                async for _ in docker_stream.stream("/events", self.docker_socket, query):
                    self._dirty = True
                    if self._wake:
                        self._wake.set()
            except (docker_stream.DockerStreamError, OSError, ValueError):
                pass
            await asyncio.sleep(5)                          # Docker restarted or isn't there yet

    async def _refresh_containers(self, now):
        self._containers_at, self._dirty = now, False
        prefixes = self.apps.companion_prefixes()
        try:
            self.containers = await asyncio.to_thread(self.docker.containers, tuple(prefixes))
            self.docker_problem = None
        except DockerUnavailable as exc:
            self.containers, self.docker_problem = [], str(exc)
        for c in self.containers:                      # an app's companions count as the app (NOTE-29)
            if not c["project"]:
                c["companion_of"] = next((app for p, app in prefixes.items() if c["name"].startswith(p)), "")
        running = {c["id"] for c in self.containers if c["state"] == "running"}
        for cid in running - set(self.readers):
            self.readers[cid] = asyncio.get_running_loop().create_task(self._read_stats(cid))
        for cid in set(self.readers) - running:
            self.readers.pop(cid).cancel()
            self.usage.pop(cid, None)
        if self.docker_problem is None and self._events is None:
            self._events = asyncio.get_running_loop().create_task(self._read_events())
        for c in self.containers:
            history = self.restarts.setdefault(c["id"], collections.deque())
            history.append((now, c.get("restart_count") or 0))
            while history and now - history[0][0] > alert_rules.RESTART_WINDOW:
                history.popleft()
        for cid in set(self.restarts) - {c["id"] for c in self.containers}:
            del self.restarts[cid]
        try:
            listing = await self.apps.list(self.containers, self.docker_problem)
            self.app_rows = listing["apps"]
        except Exception as exc:
            print(f"nimbus-control: listing apps for the dashboard failed: {exc!r}", flush=True)

    def restart_loops(self):
        by_project = {}
        for c in self.containers:
            history = self.restarts.get(c["id"])
            if history and len(history) > 1:
                grew = history[-1][1] - history[0][1]
                if grew >= alert_rules.RESTART_LIMIT:
                    by_project[c["project"]] = max(by_project.get(c["project"], 0), grew)
        return by_project

    def app_usage(self):
        out = {}
        for c in self.containers:
            u = self.usage.get(c["id"])
            app = c["project"] or c.get("companion_of")
            if not u or not app:
                continue
            row = out.setdefault(app, {"cpu": 0.0, "memory": 0, "containers": 0})
            row["cpu"] += u["cpu"]
            row["memory"] += u["memory"]
            row["containers"] += 1
        return out

    async def _stop_docker(self):
        tasks = list(self.readers.values()) + ([self._events] if self._events else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.readers.clear()
        self.usage.clear()
        self._events = None

    # --- VMs -------------------------------------------------------------------------------------------
    async def vm_usage(self, threads):
        try:
            now = self.clock()                         # timed at the call, not at the start of the sample
            stats = await asyncio.to_thread(self.libvirt.stats)
            self.libvirt_problem = None
        except LibvirtUnavailable as exc:
            self.libvirt_problem = str(exc)
            return {}, {}
        usage = {}
        for uid, s in stats.items():
            if not s.get("active"):
                self.vm_samples.pop(uid, None)
                continue
            last = self.vm_samples.get(uid)
            self.vm_samples[uid] = (now, s["cpu_time"])
            cpu = host = None
            if last and now > last[0] and s["cpu_time"] >= last[1]:
                used = (s["cpu_time"] - last[1]) / ((now - last[0]) * 1e9)
                host = min(100.0, 100.0 * used / max(1, threads))
                cpu = min(100.0, 100.0 * used / max(1, s["vcpus"]))
            usage[uid] = {"name": s["name"], "cpu": cpu, "cpu_host": host, "memory": s["rss"]}
        for uid in set(self.vm_samples) - set(stats):
            del self.vm_samples[uid]
        return usage, stats

    # --- services, updates, storage ------------------------------------------------------------------
    async def failed_units(self):
        if not os.path.isdir("/run/systemd/system"):
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                SYSTEMCTL, "list-units", "--failed", "--plain", "--no-legend", "--no-pager", *UNIT_PATTERNS,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
        except (OSError, asyncio.TimeoutError):
            return self.units
        return sorted(line.split()[0] for line in out.decode("utf-8", "replace").splitlines() if line.strip())

    async def refresh_storage(self, listing=None, parity=None):
        """Keep a copy of privd's cached disk and parity data. With LISTING,
        take what the Storage page just read instead of asking privd."""
        try:
            if listing is None:
                listing = await self.privd.call("disk.list", timeout=600)
            if parity is None and isinstance(listing, dict) and listing.get("pool", {}).get("parity_configured"):
                parity = await self.privd.call("snapraid.status", timeout=700)
        except PrivdError as exc:
            print(f"nimbus-control: refreshing disk data for the dashboard failed: {exc.message}", flush=True)
            return
        if isinstance(listing, dict):
            self.storage = {"disks": listing.get("disks", []), "fstab": listing.get("fstab", []),
                            "pool": listing.get("pool", {}), "parity": parity, "at": self.wall()}

    def start_housekeeping(self):
        async def loop():
            await asyncio.sleep(STORAGE_FIRST)
            while True:
                await self.refresh_storage()
                await asyncio.sleep(STORAGE_SECONDS)
        self._housekeeping = asyncio.get_running_loop().create_task(loop())

    def storage_now(self):
        pool = space(S.POOL) if os.path.ismount(S.POOL) else None
        parity = (self.storage or {}).get("parity")
        return {"system": space("/"), "pool": pool,
                "parity": None if not parity or not parity.get("configured") else {
                    "running": parity.get("running"), "last_sync": (parity.get("last") or {}).get("last_sync"),
                    "last_sync_ok": (parity.get("last") or {}).get("last_sync_ok"),
                    "unprotected_bytes": (parity.get("diff") or {}).get("bytes")},
                "checked": (self.storage or {}).get("at")}

    # --- one sample ------------------------------------------------------------------------------------
    async def tick(self):
        async with self._lock:
            return await self._tick()

    async def _tick(self):
        now = self.clock()
        host = await asyncio.to_thread(self.host.sample, now)
        if self._dirty or now - self._containers_at >= CONTAINERS_SECONDS:
            await self._refresh_containers(now)
        apps = self.app_usage()
        vms, vm_states = await self.vm_usage(host["threads"])
        if now - self._units_at >= UNITS_SECONDS:
            self.units, self._units_at = await self.failed_units(), now
        if now - self._updates_at >= UPDATES_SECONDS:
            self._updates_at = now
            try:
                self.update_status = await self.updates.status()
            except PrivdError:
                pass
        storage = self.storage_now()
        shares = split(host, apps, vms)
        self.alerts = alert_rules.evaluate(
            apps=self.app_rows, restart_loops=self.restart_loops(),          # a Compose project is an app's name
            vms=vm_states, storage=self.storage, space={"pool": storage["pool"], "system": storage["system"]},
            units=self.units, updates=self.update_status, limits=self.store.settings()["alerts"], now=self.wall())
        t = self.wall()
        for key, value in (("t", t), ("cpu", host["cpu"]), ("memory", host["memory"]["used"]),
                           ("net_rx", host["net"]["rx"]), ("net_tx", host["net"]["tx"])):
            self.history[key].append(value)
        self.latest = {"t": t, "host": host, "split": shares, "apps": apps, "vms": vms, "storage": storage,
                       "alerts": self.alerts, "docker": {"ok": self.docker_problem is None, "message": self.docker_problem},
                       "libvirt": {"ok": self.libvirt_problem is None, "message": self.libvirt_problem}}
        for q in list(self.queues):
            if q.full():
                q.get_nowait()                             # a slow window skips a sample, never blocks us
            q.put_nowait(self.latest)
        return self.latest

    def snapshot(self):
        return {**(self.latest or {}), "hostname": socket.gethostname(), "interval": SAMPLE_SECONDS,
                "history": {k: list(v) for k, v in self.history.items()}}


# --- routes ------------------------------------------------------------------------------------------
router = APIRouter()


@router.get("/api/dashboard")
async def dashboard(request: Request):
    svc = request.app.state.metrics
    svc.touch()
    if svc.latest is None:
        async with svc._lock:                          # the collector may be taking it right now
            if svc.latest is None:
                await svc._tick()
    return svc.snapshot()


@router.websocket("/ws/metrics")
async def metrics_socket(websocket: WebSocket):
    svc = websocket.app.state.metrics
    await websocket.accept(subprotocol=WS_PROTOCOL)
    queue = asyncio.Queue(maxsize=3)
    svc.queues.add(queue)
    svc.touch()

    async def watch():
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    watcher = asyncio.get_running_loop().create_task(watch())
    try:
        while not watcher.done():
            getter = asyncio.get_running_loop().create_task(queue.get())
            done, _ = await asyncio.wait({getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                break
            await websocket.send_json({"type": "tick", **getter.result()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        svc.queues.discard(queue)
        svc.last_request = svc.clock()                     # keep sampling 10 more seconds
        watcher.cancel()
