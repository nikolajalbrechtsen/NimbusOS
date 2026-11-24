# =============================================================================
# control-center/tests/test_metrics.py: the dashboard's collector
# =============================================================================
# What: - docker_stream against FakeDockerd on a real Unix socket: documents
#         split across chunks, a plain body, an error with Docker's message,
#         the connection closed when the reader stops;
#       - the formulas: container CPU as a share of the host (from "docker
#         stats" percent), memory = usage − inactive_file (cgroup v2 and v1),
#         host CPU from cpu_times deltas (guest time not counted twice),
#         network rates, the CPU temperature (or none), the attribution
#         (System & desktop never below 0);
#       - MetricsService.tick with fakes: one stats reader per running
#         container (and none once it stops), per-app sums, VM CPU from
#         cpu.time deltas (of the host and of the VM's vCPUs), a restart loop,
#         one hour of history;
#       - a Docker event wakes the collector at once: the alert appears well
#         within the spec's 4 seconds;
#       - sampling stops 10 seconds after the last viewer; the API: the token
#         is needed, GET /api/dashboard, WS /ws/metrics pushes samples.
# Why:  spec Dashboard; docs/PLAN.md §4.4, NOTE-69.
# =============================================================================

import asyncio
import collections
import json
import time
import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import CC, FakeDocker, FakeDockerd, FakeLibvirt, FakePrivd, container, docker_stats
from nimbus_control import docker_stream, metrics_service
from nimbus_control.app import create_app
from nimbus_control.config import Settings
from nimbus_control.metrics_service import HostSampler, MetricsService, split

GIB = 1024 ** 3


# --- the Docker stream -----------------------------------------------------------------------
def test_stream_joins_split_documents(tmp_path):
    async def main():
        d = await FakeDockerd(tmp_path / "d.sock").start()
        d.stats["abc"] = [{"n": 1}, {"n": 2}, {"n": 3}]
        got = []
        async for doc in docker_stream.stream("/containers/abc/stats", d.path, {"stream": "1"}):
            got.append(doc["n"])
            if len(got) == 3:
                break
        await asyncio.sleep(0.1)
        open_after = d.open
        await d.stop()
        return got, d.requests, open_after
    got, requests, open_after = asyncio.run(main())
    assert got == [1, 2, 3] and requests == ["/containers/abc/stats?stream=1"]
    assert open_after == 0                                    # stopping the reader closed the connection


def test_stream_errors_and_plain_bodies(tmp_path):
    async def answer(body, head):
        async def handle(reader, writer):
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            writer.write(head + body)
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(handle, str(tmp_path / "p.sock"))
        try:
            return [d async for d in docker_stream.stream("/x", str(tmp_path / "p.sock"))]
        finally:
            server.close()
    plain = asyncio.run(answer(b'{"a": 1}\n{"a": 2}', b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"))
    assert plain == [{"a": 1}, {"a": 2}]
    with pytest.raises(docker_stream.DockerStreamError) as err:
        asyncio.run(answer(b'{"message": "No such container: x"}', b"HTTP/1.1 404 Not Found\r\nContent-Length: 35\r\n\r\n"))
    assert "404: No such container: x" in str(err.value)
    with pytest.raises(docker_stream.DockerStreamError) as err:
        asyncio.run(docker_stream.stream("/x", str(tmp_path / "missing.sock")).__anext__())
    assert "socket can't be opened" in str(err.value)


def test_container_formulas():
    # 0.4 s of CPU in 2 s on 4 CPUs: "docker stats" shows 20 %, which is 5 % of the host.
    doc = docker_stats(total=1_400_000_000, system=18_000_000_000, pre_total=1_000_000_000, pre_system=10_000_000_000)
    assert docker_stream.cpu_share(doc) == pytest.approx(5.0)
    assert docker_stream.cpu_share(docker_stats(total=5, system=0)) == 0.0               # the first sample
    assert docker_stream.memory_used(docker_stats(0, 0, usage=600 * 2 ** 20, inactive=200 * 2 ** 20)) == 400 * 2 ** 20
    assert docker_stream.memory_used(docker_stats(0, 0, usage=600, inactive=100, cgroup=1)) == 500
    assert docker_stream.memory_used({"memory_stats": {}}) == 0
    old = docker_stats(total=0, system=0)
    old["cpu_stats"].pop("online_cpus")
    old["cpu_stats"]["cpu_usage"]["percpu_usage"] = [0, 0]
    assert docker_stream.cpu_share(old) == 0.0


# --- the host -----------------------------------------------------------------------------------
T = collections.namedtuple("T", "user nice system idle iowait irq softirq steal guest guest_nice")
Mem = collections.namedtuple("Mem", "total available")
Swap = collections.namedtuple("Swap", "total used")
Net = collections.namedtuple("Net", "bytes_recv bytes_sent")
Temp = collections.namedtuple("Temp", "label current")


class FakePs:
    def __init__(self):
        self.times = [T(100, 0, 50, 850, 0, 0, 0, 0, 0, 0)]
        self.cores = [[T(50, 0, 25, 425, 0, 0, 0, 0, 0, 0)] * 2]
        self.net = {"eth0": Net(1_000_000, 500_000)}
        self.temps = {"coretemp": [Temp("Core 0", 50.0), Temp("Package id 0", 54.25)]}

    def cpu_times(self, percpu=False):
        return self.cores[-1] if percpu else self.times[-1]

    def virtual_memory(self):
        return Mem(16 * GIB, 10 * GIB)

    def swap_memory(self):
        return Swap(2 * GIB, 0)

    def net_io_counters(self, pernic=False):
        return self.net

    def sensors_temperatures(self):
        return self.temps

    def cpu_count(self):
        return 8


def test_host_sampler():
    ps = FakePs()
    host = HostSampler(ps, iface=lambda: "eth0")
    first = host.sample(100.0)
    assert first["cpu"] == 0.0 and first["net"] == {"iface": "eth0", "rx": 0.0, "tx": 0.0} and first["temperature"] == 54.2
    # 200 busy (user 150 + guest 50 counted once) of 1000 → 20 %
    ps.times.append(T(250, 0, 100, 1650, 0, 0, 0, 0, 50, 0))
    ps.cores.append([T(100, 0, 50, 850, 0, 0, 0, 0, 0, 0), T(50, 0, 25, 925, 0, 0, 0, 0, 0, 0)])
    ps.net = {"eth0": Net(3_000_000, 600_000)}
    second = host.sample(102.0)
    assert second["cpu"] == pytest.approx(20.0) and second["cores"] == [pytest.approx(15.0), pytest.approx(0.0)]
    assert second["net"]["rx"] == pytest.approx(1_000_000) and second["net"]["tx"] == pytest.approx(50_000)
    assert second["memory"] == {"total": 16 * GIB, "used": 6 * GIB, "available": 10 * GIB} and second["threads"] == 8
    ps.temps = {"acpitz": [Temp("", 41.0), Temp("", 47.5)]}
    assert host.temperature() == 47.5
    ps.temps = {}
    assert host.temperature() is None


def test_split_never_below_zero():
    host = {"cpu": 30.0, "memory": {"total": 16 * GIB, "used": 6 * GIB}}
    s = split(host, {"jellyfin": {"cpu": 12.0, "memory": 1 * GIB}}, {"vm": {"cpu_host": 10.0, "memory": 2 * GIB}})
    assert s["cpu"] == {"system": pytest.approx(8.0), "docker": 12.0, "vms": 10.0, "free": pytest.approx(70.0)}
    assert s["memory"] == {"total": 16 * GIB, "system": 3 * GIB, "docker": GIB, "vms": 2 * GIB, "free": 10 * GIB}
    over = split(host, {"a": {"cpu": 40.0, "memory": 9 * GIB}}, {})
    assert over["cpu"]["system"] == 0.0 and over["memory"]["system"] == 0 and over["cpu"]["free"] == pytest.approx(60.0)


# --- the collector --------------------------------------------------------------------------------
class FakeHost:
    def __init__(self):
        self.cpu = 25.0

    def sample(self, now):
        return {"cpu": self.cpu, "cores": [self.cpu], "threads": 4, "load": [0.5, 0.4, 0.3],
                "memory": {"total": 16 * GIB, "used": 8 * GIB, "available": 8 * GIB}, "swap": {"total": 0, "used": 0},
                "net": {"iface": "eth0", "rx": 1000.0, "tx": 500.0}, "temperature": None}


class FakeApps:
    def __init__(self):
        self.rows = []
        self.calls = 0

    async def list(self, containers=None, problem=None):
        self.calls += 1
        return {"apps": self.rows, "docker": {"ok": problem is None, "message": problem}}

    def companion_prefixes(self):
        return {"nextcloud-aio-": "cloud"}


class FakeUpdates:
    """UpdatesService as the collector sees it: status(), cached() and version."""

    def __init__(self):
        self.version, self._cached, self.calls = 0, None, 0

    async def status(self):
        self.calls += 1
        return {"counts": {"security": 0}, "checked_at": "2026-11-23T08:00:00Z", "errors": []}

    def cached(self):
        return self._cached


def collector(tmp_path, docker=None, libvirt=None, clock=None, wall=None):
    store = types.SimpleNamespace(settings=lambda: {"alerts": {"hdd_temp_c": 50, "nvme_temp_c": 70}})
    return MetricsService(store=store, privd=FakePrivd(), docker=docker or FakeDocker(), libvirt=libvirt or FakeLibvirt(),
                          apps=FakeApps(), updates=FakeUpdates(), host=FakeHost(), docker_socket=str(tmp_path / "d.sock"),
                          clock=clock or time.monotonic, wall=wall or time.time)


def test_tick_with_containers(tmp_path):
    async def main():
        d = await FakeDockerd(tmp_path / "d.sock").start()
        d.stats["c1"] = [docker_stats(total=1_400_000_000, system=18_000_000_000, pre_total=1_000_000_000,
                                      pre_system=10_000_000_000, usage=300 * 2 ** 20, inactive=100 * 2 ** 20)]
        d.stats["c2"] = [docker_stats(total=2_200_000_000, system=18_000_000_000, pre_total=1_000_000_000,
                                      pre_system=10_000_000_000, usage=100 * 2 ** 20)]
        docker = FakeDocker([{**container("jellyfin", "web"), "id": "c1"}, {**container("jellyfin", "db"), "id": "c2"},
                             {**container("old", "web", state="exited"), "id": "c3"}])
        svc = collector(tmp_path, docker=docker)
        await svc.tick()
        await asyncio.sleep(0.2)                      # the readers' first documents
        latest = await svc.tick()
        readers = sorted(svc.readers)
        docker.items[1]["state"] = "exited"           # c2 stops
        svc._dirty = True
        await svc.tick()
        after = sorted(svc.readers)
        await svc.stop()
        await d.stop()
        return latest, readers, after, d.requests
    latest, readers, after, requests = asyncio.run(main())
    assert readers == ["c1", "c2"] and after == ["c1"]
    jelly = latest["apps"]["jellyfin"]
    assert jelly["cpu"] == pytest.approx(5.0 + 15.0) and jelly["memory"] == 300 * 2 ** 20 and jelly["containers"] == 2
    assert latest["split"]["cpu"]["docker"] == pytest.approx(20.0) and latest["split"]["cpu"]["system"] == pytest.approx(5.0)
    assert "/containers/c3/stats?stream=1" not in requests and any(r.startswith("/events?filters=") for r in requests)
    assert latest["docker"] == {"ok": True, "message": None}


def test_companions_count_as_their_app(tmp_path):
    # Nextcloud AIO's mastercontainer (Compose project "cloud") starts
    # nextcloud-aio-* containers itself, without Compose labels (NOTE-29).
    async def main():
        d = await FakeDockerd(tmp_path / "d.sock").start()
        for cid in ("c1", "c2", "c3"):
            d.stats[cid] = [docker_stats(total=1_400_000_000, system=18_000_000_000, pre_total=1_000_000_000,
                                         pre_system=10_000_000_000, usage=100 * 2 ** 20)]
        docker = FakeDocker([{**container("cloud", "mastercontainer"), "id": "c1", "name": "nextcloud-aio-mastercontainer"},
                             {**container("", "x"), "id": "c2", "name": "nextcloud-aio-apache"},
                             {**container("", "x"), "id": "c3", "name": "someone-else"}])
        svc = collector(tmp_path, docker=docker)
        await svc.tick()
        await asyncio.sleep(0.2)
        latest = await svc.tick()
        await svc.stop()
        await d.stop()
        return latest, docker
    latest, docker = asyncio.run(main())
    assert latest["apps"] == {"cloud": {"cpu": pytest.approx(10.0), "memory": 200 * 2 ** 20, "containers": 2}}
    assert latest["split"]["cpu"]["docker"] == pytest.approx(10.0)      # the unlabelled stranger isn't listed at all


def test_vm_usage_and_history(tmp_path):
    clock = [1000.0]
    lv = FakeLibvirt()
    lv.vms["web"] = {"xml": "<domain><vcpu>2</vcpu></domain>", "state": "running", "autostart": False, "uuid": "u1",
                     "cpu_time": 0, "rss": 3 * GIB}
    lv.vms["off"] = {"xml": "<domain><vcpu>1</vcpu></domain>", "state": "crashed", "autostart": False, "uuid": "u2",
                     "cpu_time": 0}

    async def main():
        svc = collector(tmp_path, libvirt=lv, clock=lambda: clock[0])
        svc.docker.available = False
        first = await svc.tick()
        clock[0] += 2.0
        lv.vms["web"]["cpu_time"] += 3_000_000_000             # 3 s of CPU in 2 s
        second = await svc.tick()
        for _ in range(metrics_service.HISTORY + 5):
            await svc.tick()
        await svc.stop()
        return first, second, svc
    first, second, svc = asyncio.run(main())
    assert first["vms"]["u1"]["cpu"] is None and "u2" not in first["vms"]
    vm = second["vms"]["u1"]
    assert vm["cpu"] == pytest.approx(75.0) and vm["cpu_host"] == pytest.approx(37.5) and vm["memory"] == 3 * GIB
    assert second["split"]["cpu"]["vms"] == pytest.approx(37.5) and second["split"]["cpu"]["system"] == 0.0
    assert [a["id"] for a in second["alerts"] if a["id"].startswith("vm:")] == ["vm:u2"]
    assert second["docker"]["ok"] is False and "docker" in second["docker"]["message"]
    assert len(svc.history["cpu"]) == metrics_service.HISTORY


def test_samples_never_overlap(tmp_path):
    # The first GET /api/dashboard takes a sample while the loop it starts
    # takes another: interleaved, one divided a CPU delta by ~0 seconds.
    class Slow(FakeLibvirt):
        active = peak = 0

        def stats(self):
            Slow.active += 1
            Slow.peak = max(Slow.peak, Slow.active)
            time.sleep(0.05)
            Slow.active -= 1
            return super().stats()

    async def main():
        svc = collector(tmp_path, libvirt=Slow())
        svc.docker.available = False
        await asyncio.gather(svc.tick(), svc.tick(), svc.tick())
        await svc.stop()
    asyncio.run(main())
    assert Slow.peak == 1


def test_restart_loop(tmp_path):
    clock = [0.0]

    async def main():
        docker = FakeDocker([{**container("bad", "web", state="running"), "id": "b1", "restart_count": 0}])
        svc = collector(tmp_path, docker=docker, clock=lambda: clock[0])
        svc.apps.rows = [{"name": "bad", "app_name": "Bad App", "state": "running", "reason": "", "busy": None}]
        await svc.tick()
        for count in (1, 2, 3):
            clock[0] += 60
            docker.items[0]["restart_count"] = count
            latest = await svc.tick()
        clock[0] += 400                                  # five quiet minutes later
        quiet = await svc.tick()
        await svc.stop()
        return latest, quiet
    latest, quiet = asyncio.run(main())
    assert latest["alerts"][0]["title"] == "bad keeps restarting (3 restarts in 5 minutes)"
    assert not any(a["id"] == "app:bad" for a in quiet["alerts"])


def test_an_event_wakes_the_collector(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_service, "SAMPLE_SECONDS", 5.0)

    async def main():
        d = await FakeDockerd(tmp_path / "d.sock").start()
        docker = FakeDocker([{**container("jelly", "web"), "id": "j1"}])
        d.stats["j1"] = [docker_stats(0, 0)]
        svc = collector(tmp_path, docker=docker)
        svc.apps.rows = [{"name": "jelly", "app_name": "Jellyfin", "state": "running", "reason": "", "busy": None}]
        q = asyncio.Queue()
        svc.queues.add(q)
        svc.touch()
        await q.get()
        await asyncio.sleep(0.3)                          # the events reader is connected
        docker.items[0].update(state="exited", exit_code=137)
        svc.apps.rows = [{"name": "jelly", "app_name": "Jellyfin", "state": "error", "busy": None,
                          "reason": "web exited unexpectedly (exit code 137)."}]
        started = time.monotonic()
        d.push_event({"Type": "container", "Action": "die", "Actor": {"ID": "j1"}})
        while True:
            tick = await asyncio.wait_for(q.get(), 5)
            if any(a["id"] == "app:jelly" for a in tick["alerts"]):
                break
        seconds = time.monotonic() - started
        svc.queues.discard(q)
        await svc.stop()
        await d.stop()
        return seconds
    seconds = asyncio.run(main())
    assert seconds < 1.5                                  # the spec allows 4; the next timed sample was 5 s away


def test_sampling_stops_without_viewers(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_service, "SAMPLE_SECONDS", 0.05)
    monkeypatch.setattr(metrics_service, "IDLE_STOP", 0.3)

    async def main():
        svc = collector(tmp_path)
        svc.docker.available = False
        svc.touch()
        await asyncio.sleep(0.15)
        running = not svc._task.done()
        await asyncio.sleep(0.6)
        stopped = svc._task.done()
        count = len(svc.history["cpu"])
        await asyncio.sleep(0.2)
        return running, stopped, count, len(svc.history["cpu"])
    running, stopped, count, later = asyncio.run(main())
    assert running and stopped and count == later and count >= 3


def test_storage_copy(tmp_path):
    async def main():
        svc = collector(tmp_path)
        svc.privd.results["disk.list"] = {"disks": [{"path": "/dev/sda", "model": "WD", "serial": "S1",
                                                     "smart": {"passed": False}}],
                                          "fstab": [], "pool": {"parity_configured": True}}
        svc.privd.results["snapraid.status"] = {"configured": True, "running": False, "last": {"last_sync_ok": True,
                                                "last_sync": "2026-11-23T03:30:00Z"}, "diff": {"bytes": 5}}
        await svc.refresh_storage()
        svc.docker.available = False
        latest = await svc.tick()
        await svc.stop()
        return latest, [v for v, _ in svc.privd.calls]
    latest, verbs = asyncio.run(main())
    assert verbs == ["disk.list", "snapraid.status"]
    assert latest["alerts"][0]["id"] == "smart:S1" and latest["storage"]["parity"]["last_sync_ok"] is True
    assert latest["storage"]["system"]["total"] > 0


# --- the API --------------------------------------------------------------------------------------
@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_service, "SAMPLE_SECONDS", 0.1)
    settings = Settings(run_dir=str(tmp_path), state_dir=str(tmp_path), www_dir=str(CC / "frontend"),
                        catalog_dir=str(tmp_path / "none"), isos_dir=str(tmp_path / "none"), vnc_tokens_dir=str(tmp_path / "none"),
                        docker_socket=str(tmp_path / "none.sock"))
    app = create_app(settings, privd=FakePrivd(), font_lister=lambda p: [], docker=FakeDocker(), libvirt=FakeLibvirt(),
                     host_sampler=FakeHost())
    with TestClient(app, base_url="http://127.0.0.1:7777") as client:
        yield types.SimpleNamespace(app=app, client=client, h={"Authorization": f"Bearer {app.tokens.token}"})


def test_dashboard_api(w):
    assert w.client.get("/api/dashboard").status_code == 401
    r = w.client.get("/api/dashboard", headers=w.h).json()
    assert r["host"]["cpu"] == 25.0 and r["interval"] == 0.1 and len(r["history"]["cpu"]) >= 1
    assert set(r) >= {"split", "apps", "vms", "storage", "alerts", "docker", "libvirt", "hostname", "history"}


def test_metrics_websocket(w):
    protocols = ["nimbus.v1", f"nimbus.token.{w.app.tokens.token}"]
    with w.client.websocket_connect("ws://127.0.0.1:7777/ws/metrics", subprotocols=protocols,
                                    headers={"origin": "http://127.0.0.1:7777"}) as ws:
        first, second = ws.receive_json(), ws.receive_json()
    assert first["type"] == "tick" and second["t"] > first["t"] and first["host"]["memory"]["total"] == 16 * GIB
    with pytest.raises(WebSocketDisconnect):
        with w.client.websocket_connect("ws://127.0.0.1:7777/ws/metrics", subprotocols=["nimbus.v1"],
                                        headers={"origin": "http://127.0.0.1:7777"}) as ws:
            ws.receive_json()


def test_json_safe(w):
    r = w.client.get("/api/dashboard", headers=w.h)
    json.loads(r.text)


def test_update_alerts_follow_the_updates_page_at_once(tmp_path):
    # Found in the QEMU test (NOTE-72): after "Check now" found a release, the
    # dashboard kept saying "Updates have never been checked" for minutes.
    async def main():
        svc = collector(tmp_path)
        await svc.tick()
        first = svc.updates.calls
        await svc.tick()
        assert svc.updates.calls == first                    # nothing changed: no new privd call
        svc.updates._cached = {"counts": {}, "update_available": True, "latest": {"version": "0.1.1-beta.1"},
                               "checked_at": "2026-11-24T06:09:00Z", "errors": []}
        svc.updates.version += 1                             # the Updates page checked
        await svc.tick()
        assert "NimbusOS 0.1.1-beta.1 is available" in [a["title"] for a in svc.alerts]
        assert svc.updates.calls == first                    # its fresh status was used as it is
        svc.updates._cached = None
        svc.updates.version += 1                             # an install finished: the status was invalidated
        await svc.tick()
        assert svc.updates.calls == first + 1                # so it is read again
        assert not [a for a in svc.alerts if a["id"] == "updates:release"]
    asyncio.run(main())
