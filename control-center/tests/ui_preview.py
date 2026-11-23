# =============================================================================
# control-center/tests/ui_preview.py: screenshots of the Control Center UI
# =============================================================================
# What: serves the real frontend and backend on 127.0.0.1:7777 and takes
#       screenshots with headless Chromium.
#       Default mode, on made-up data: FakePrivd (canned update status and
#       history), FakeDocker (four demo apps: running, error, starting and
#       stopped; one with a template update; the failing one's host port held
#       by a listener here, for the Dashboard's Fix) with a FakeDockerd thread
#       streaming their stats, PreviewHost (an hour of gently drifting
#       history), the fixture catalog, and a
#       FakeLibvirt with four VMs (running Linux with usage, paused on a disk
#       error, stopped Windows that starts with the computer, one made by
#       another tool) and three ISOs:
#         - each page given on the command line at 1400x900, at full height
#           (1400x1800) and at 800 px (the narrow layout);
#         - "scenes": a page plus buttons and labels to press, in order (by
#           their text or, for icon buttons, their label; "@N" for the Nth
#           match, a trailing "*" for "starts with"): the Apps catalog,
#           Advanced install, its Compose tab, the template diff; the ISO
#           library, Create VM (Linux and Windows), VM options, deleting a VM;
#           the Disks, Pool and Users tabs, a share's editor, formatting a
#           disk, and the NAS wizard on a NAS not set up yet (PreviewNas: a
#           pool with parity, a disk with bad sectors, a sleeping one);
#         - the signed-out page.
#       --live-vm mode (inside tests/integration/vm-libvirt.sh, with libvirtd
#       running): the real libvirt, a real websockify, one real VM booting
#       its UEFI firmware; screenshots of the VM list and of its console
#       open in the page, taken over the DevTools protocol after real
#       seconds, because the console is a live WebSocket stream.
#       Animations are off (reduced motion). The frontend is copied to a
#       temporary folder with preview-actions.js added before app.js: it
#       reads ?click= before app.js removes the query and presses the buttons
#       once they appear. Each screenshot signs in with its own one-time
#       launch code, as the launcher does.
# Why:  the design system is checked by looking at it (spec "UI design
#       system"); used by developers and CI, never installed.
#
# Usage: python3 control-center/tests/ui_preview.py OUT_DIR [PAGE ...]
#        python3 control-center/tests/ui_preview.py --live-vm OUT_DIR
#        Needs chromium, python3-uvicorn, python3-websockets, fonts-inter,
#        fonts-jetbrains-mono; --live-vm also libvirtd, websockify, novnc.
# =============================================================================

import asyncio
import base64
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from conftest import (CC, FIXTURE_CATALOG, FakeDocker, FakeDockerd, FakeLibvirt, FakeNas, FakePrivd, container,  # noqa: E402
                      docker_stats, nas_disk)
from nimbus_common import shares as S  # noqa: E402
from nimbus_control import compose, vm_xml  # noqa: E402
from nimbus_control.app import create_app  # noqa: E402
from nimbus_control.catalog import Catalog  # noqa: E402
from nimbus_control.config import Settings  # noqa: E402

GIB = 1024 ** 3
HISTORY = {"entries": [
    {"start": "2026-11-17  06:25:12", "end": "2026-11-17  06:26:40", "command": "apt-get -y -q full-upgrade",
     "requested_by": "", "error": None, "changes": {"upgrade": [{"name": "openssl", "versions": ["3.5.1-1"], "automatic": False}] * 4}},
    {"start": "2026-11-15  06:12:03", "end": "2026-11-15  06:12:30", "command": "/usr/bin/unattended-upgrade",
     "requested_by": "", "error": None, "changes": {"upgrade": [{"name": "libc6", "versions": [], "automatic": False}]}},
]}

SCENES = {
    "apps-catalog": ("apps", ["Catalog"]),
    "apps-advanced": ("apps", ["Catalog", "Advanced…@2"]),
    "apps-compose": ("apps", ["Catalog", "Advanced…@2", "Compose file"]),
    "apps-template": ("apps", ["Template update"]),
    "vms-isos": ("vms", ["ISO library"]),
    "vms-create": ("vms", ["Create VM"]),
    "vms-create-windows": ("vms", ["Create VM", "Windows*"]),
    "vms-options": ("vms", ["Options: CD drive, start with the computer@2"]),
    "vms-delete": ("vms", ["Delete@0"]),
    "dashboard-fix": ("dashboard", ["Fix@0"]),
    "storage-disks": ("storage", ["Disks"]),
    "storage-pool": ("storage", ["Pool"]),
    "storage-users": ("storage", ["Users"]),
    "storage-share": ("storage", ["Edit media"]),
    "storage-format": ("storage", ["Disks", "Format…@0"]),
}

ACTIONS_JS = """// preview-actions.js: presses buttons for ui_preview.py screenshots (never installed).
const wanted = (new URLSearchParams(location.search).get("click") || "").split("|").filter(Boolean);
async function press(spec) {
  const [text, nth] = spec.split("@");
  const prefix = text.endsWith("*") ? text.slice(0, -1) : null;
  const label = (b) => b.textContent.trim() || b.getAttribute("aria-label") || "";
  for (let i = 0; i < 100; i += 1) {
    const matches = [...document.querySelectorAll("button, label")]
      .filter((b) => (prefix !== null ? label(b).startsWith(prefix) : label(b) === text));
    if (matches[Number(nth || 0)]) { matches[Number(nth || 0)].click(); return; }
    await new Promise((r) => setTimeout(r, 50));
  }
}
(async () => { for (const spec of wanted) { await press(spec); await new Promise((r) => setTimeout(r, 400)); } })();
"""


# --- made-up data ----------------------------------------------------------------------------
def build_state(tmp, catalog_dir):
    """Four installed demo apps, their Compose files, and two shares."""
    state = tmp / "state"
    stacks = state / "stacks"
    stacks.mkdir(parents=True)
    media = tmp / "mnt" / "user" / "Media"
    media.mkdir(parents=True)
    (state / "shares.json").write_text(json.dumps({"shares": [{"name": "Media", "path": str(media)},
                                                              {"name": "Backups", "path": str(tmp / "mnt" / "user" / "Backups")}]}))
    catalog = Catalog(str(catalog_dir))
    port = lambda pid, container_port, host, proto="tcp", ip="": {"id": pid, "container": container_port, "host": host, "protocol": proto, "ip": ip}
    apps = {
        "demo-web": ("demo-web", 1, [port("http", 80, 8080)],
                     [{"id": "config", "container": "/config", "source": str(stacks / "demo-web" / "config"), "read_only": False},
                      {"id": "media", "container": "/media", "source": str(media), "read_only": False}],
                     {"TZ": "Europe/Copenhagen", "ADMIN_PASSWORD": "kV3xq9Lr2tW8nZpA5sYb1cHd"}, "bridge", "running"),
        "dns": ("demo-dns", 1, [port("dns_udp", 53, 53, "udp", "192.168.1.20"), port("dns_tcp", 53, 53, "tcp", "192.168.1.20"),
                                port("admin", 80, 8081)],
                [{"id": "data", "container": "/etc/dns", "source": "dnsdata", "read_only": False}],
                {"WEBPASSWORD": "9f2c4e6a8b0d1f3e5a7c9b1d3f5e7a9c"}, "bridge", "running"),
        "monitor": ("demo-host", 1, [], [{"id": "data", "container": "/data", "source": str(stacks / "monitor" / "data"),
                                          "read_only": False}], {}, "host", "running"),
        "demo-web-2": ("demo-web", 2, [port("http", 80, 8082)],
                       [{"id": "config", "container": "/config", "source": str(stacks / "demo-web-2" / "config"), "read_only": False}],
                       {"TZ": "Etc/UTC", "ADMIN_PASSWORD": "Qm7Tz1Kc8Vb3Xn6Lw2Pd9Rf4"}, "bridge", "stopped"),
    }
    records = {}
    for name, (app_id, version, ports, volumes, env, mode, desired) in apps.items():
        entry = catalog.get(app_id)
        inputs = {"name": name, "ports": ports, "volumes": volumes, "env": env, "network_mode": mode, "restart": "unless-stopped"}
        template = entry["template"]
        if version == 1 and app_id == "demo-web":           # what template version 1 looked like
            template = "\n".join(line for line in template.splitlines() if "PUBLIC_URL" not in line) + "\n"
        text, _ = compose.build(entry["manifest"], template, inputs, host_ip="192.168.1.20", hostname="nimbus")
        (stacks / name).mkdir()
        (stacks / name / "docker-compose.yml").write_text(text)
        records[name] = {"app": app_id, "template_version": version, "inputs": inputs, "desired": desired, "custom": False,
                         "created": "2026-11-10T08:00:00+00:00", "updated": "2026-11-10T08:00:00+00:00"}
    (state / "stacks.json").write_text(json.dumps({"stacks": records}, indent=2))
    docker = FakeDocker([
        container("demo-web", "web", ports=[("tcp", 8080)]),
        container("dns", "dns", state="exited", exit_code=1),
        container("monitor", "monitor", health="starting"),
        container("demo-web-2", "web", state="exited", exit_code=0),
    ])
    return state, docker


class PreviewLibvirt(FakeLibvirt):
    """FakeLibvirt whose running VMs use a steady share of their vCPUs."""

    def __init__(self, load, **kw):
        super().__init__(**kw)
        self.load = load
        self._last = time.monotonic()

    def stats(self):
        now = time.monotonic()
        for name, vm in self.vms.items():
            vcpus = int(vm["xml"].split("<vcpu")[1].split(">")[1].split("<")[0])
            vm["cpu_time"] += int((now - self._last) * 1e9 * vcpus * self.load.get(name, 0))
        self._last = now
        return super().stats()


def build_vms(tmp):
    isos = tmp / "isos"
    isos.mkdir()
    for name, size in (("debian-13.2.0-amd64-netinst.iso", 0.75 * GIB), ("Win11_24H2_English_x64.iso", 5.8 * GIB),
                       ("virtio-win-0.1.266.iso", 0.71 * GIB)):
        with open(isos / name, "wb") as f:
            f.truncate(int(size))
    lv = PreviewLibvirt({"debian-server": 0.23, "home-assistant": 0.06}, threads=16, memory=64 * GIB)
    vms = [("debian-server", "linux", 4, 8192, "uefi", "debian-13.2.0-amd64-netinst.iso", None, 64, "running", False),
           ("home-assistant", "linux", 2, 4096, "uefi", None, None, 32, "running", True),
           ("win11", "windows", 4, 8192, "uefi", "Win11_24H2_English_x64.iso", "virtio-win-0.1.266.iso", 80, "stopped", True),
           ("backup-test", "linux", 1, 2048, "bios", None, None, 16, "paused", False)]
    for i, (name, preset, vcpus, mem, fw, iso, drivers, disk, st, auto) in enumerate(vms):
        volume, _ = lv.create_volume(name, disk * GIB)
        lv.define(vm_xml.build_domain({
            "name": name, "uuid": f"6c2f0a1e-0000-4000-8000-00000000000{i}", "preset": preset, "vcpus": vcpus, "memory_mib": mem,
            "firmware": fw, "pool": "nimbus-images", "volume": volume, "iso": iso and str(isos / iso),
            "drivers_iso": drivers and str(isos / drivers), "network": {"mode": "nat"}, "vnc_password": "Pr3view0",
            "created": "2026-11-10T08:00:00+00:00"}))
        lv.vms[name].update(state=st, autostart=auto)
    lv.vms["backup-test"].update(reason="paused by a disk error (is the disk full?)", problem=True)
    foreign = (pathlib.Path(__file__).parent / "test_vm_xml.py").read_text().split('FOREIGN = """')[1].split('"""')[0]
    lv.vms["legacy-vm"] = {"xml": foreign.replace("old-vm", "legacy-vm"), "state": "stopped", "autostart": False,
                           "uuid": "11111111-2222-3333-4444-555555555555", "cpu_time": 0}
    return isos, lv


# --- serving and shooting --------------------------------------------------------------------------
def prepare_www(tmp):
    www = tmp / "www"
    shutil.copytree(CC / "frontend", www)
    (www / "preview-actions.js").write_text(ACTIONS_JS)
    index = (www / "index.html").read_text()
    (www / "index.html").write_text(index.replace('<script type="module" src="/app.js">',
                                                  '<script type="module" src="/preview-actions.js"></script>\n  '
                                                  '<script type="module" src="/app.js">'))
    (tmp / "os-release").write_text('NAME="NimbusOS"\nVERSION="0.1.0 (stable)"\nVERSION_ID="0.1.0"\n'
                                    "NIMBUSOS_CHANNEL=stable\nNIMBUSOS_DEB_VERSION=\"0.1.0\"\n")
    return www


def serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=7777, log_level="warning", ws="websockets"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:7777/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    return server


def page_url(codes, page=None, clicks=()):
    query = {"launch": codes.issue()} if page else {}
    if clicks:
        query["click"] = "|".join(clicks)
    return "http://127.0.0.1:7777/" + (f"?{urllib.parse.urlencode(query)}#{page}" if page else "")


CHROMIUM = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-prefers-reduced-motion"]


def shot(path, url, width=1400, height=900):
    subprocess.run([*CHROMIUM, f"--window-size={width},{height}", "--virtual-time-budget=8000", f"--screenshot={path}", url],
                   check=True, capture_output=True, timeout=120)
    print(f"wrote {path}")


def live_shot(path, url, width=1400, height=900, wait=10.0):
    """A screenshot after WAIT real seconds, over the DevTools protocol."""
    import websockets.sync.client as wsc
    proc = subprocess.Popen([*CHROMIUM, f"--window-size={width},{height}", "--remote-debugging-port=9223", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        targets = []
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen("http://127.0.0.1:9223/json", timeout=1))
                if any(t["type"] == "page" for t in targets):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        target = next(t for t in targets if t["type"] == "page")
        with wsc.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            counter = [0]

            def call(method, **params):
                counter[0] += 1
                ws.send(json.dumps({"id": counter[0], "method": method, "params": params}))
                while True:
                    message = json.loads(ws.recv(timeout=60))
                    if message.get("id") == counter[0]:
                        return message.get("result", {})
            call("Page.enable")
            call("Page.navigate", url=url)
            time.sleep(wait)
            data = call("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(data))
        print(f"wrote {path}")
    finally:
        proc.terminate()
        proc.wait()


class PreviewNas(FakeNas):
    """A NAS in Pooled + parity mode: two data disks, a parity disk, two new disks."""

    def __init__(self):
        super().__init__()
        smart = lambda t, **kw: {"passed": True, "temperature": t, "asleep": False, "nvme": False, **kw}  # noqa: E731
        self.disks = [
            {**nas_disk("nvme0n1", role="system", system=True, size=1000 ** 4, fstype="vfat"), "model": "Samsung SSD 980 1TB",
             "serial": "S649NX0T123456", "mounts": ["/boot/efi", "/"], "smart": smart(38, nvme=True)},
            {**nas_disk("sda", "data", "disk1", fstype="xfs", size=8 * 1000 ** 4), "model": "WDC WD80EFZZ", "serial": "WD-CA1B2C3D",
             "mounts": ["/mnt/disks/disk1"], "smart": smart(36)},
            {**nas_disk("sdb", "data", "disk2", fstype="xfs"), "model": "ST4000VN008", "serial": "ZDH1A2B3",
             "mounts": ["/mnt/disks/disk2"], "smart": smart(41, reallocated=8)},
            {**nas_disk("sdc", "parity", "parity1", fstype="xfs", size=8 * 1000 ** 4), "model": "WDC WD80EFZZ", "serial": "WD-CA9Z8Y7X",
             "mounts": ["/mnt/parity/parity1"], "smart": {"passed": True, "temperature": 35, "asleep": True}},
            {**nas_disk("sdd", fstype="ntfs", size=8 * 1000 ** 4), "model": "WDC WD80EFZZ", "serial": "WD-CA5E6F7G",
             "partitions": [{"path": "/dev/sdd1", "size": 8 * 1000 ** 4, "fstype": "ntfs", "label": "Backup", "uuid": "u", "mountpoints": []}],
             "smart": smart(33)},
            {**nas_disk("sde", size=12 * 1000 ** 4), "model": "ST12000VN0008", "serial": "ZL2X9Y8W", "smart": smart(34)},
        ]
        self.fstab = [{"kind": "disk", "role": "data", "label": "disk1", "uuid": "u1", "fs": "xfs", "present": True},
                      {"kind": "disk", "role": "data", "label": "disk2", "uuid": "u2", "fs": "xfs", "present": True},
                      {"kind": "disk", "role": "parity", "label": "parity1", "uuid": "u3", "fs": "xfs", "present": True},
                      {"kind": "pool", "branches": ["disk1", "disk2"], "policy": "mfs"}]
        self.parity_configured = True
        self.users = {"alice": {"uid": 120, "managed": True, "desktop": False},
                      "bob": {"uid": 121, "managed": True, "desktop": False},
                      "sam": {"uid": 1000, "managed": False, "desktop": True}}

    def disk_list(self, args):
        out = super().disk_list(args)
        out["pool"]["usage"] = {"total": 12 * 1000 ** 4, "free": int(7.4 * 1000 ** 4)}
        return out

    def snapraid_status(self, args):
        synced = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 6 * 3600))
        return {"configured": True, "installed": True, "running": False, "diff": {"unprotected": 42},
                "last": {"last_sync": synced, "last_sync_ok": True, "last_scrub": synced, "last_scrub_ok": True}}


def build_nas(tmp, state):
    for name in ("DISKS", "PARITY", "POOL", "SYSTEM_SHARES"):
        path = tmp / "nas" / name.lower()
        path.mkdir(parents=True)
        setattr(S, name, str(path))
    mounted = {S.POOL, f"{S.DISKS}/disk1", f"{S.DISKS}/disk2"}
    real = os.path.ismount
    os.path.ismount = lambda p: p in mounted or real(p)
    specs = [S.share_spec(s, "") for s in [
        {"name": "media", "location": "pool", "security": "secure", "users": {"alice": "rw", "sam": "rw"},
         "recycle": {"enabled": True, "days": 30}, "notes": "Films, series and music for Jellyfin."},
        {"name": "documents", "location": "pool", "security": "private", "users": {"alice": "rw", "bob": "ro"},
         "recycle": {"enabled": True, "days": 30}},
        {"name": "backups", "location": "pool", "security": "private", "users": {"sam": "rw"}},
        {"name": "timemachine", "location": "pool", "security": "private", "users": {"alice": "rw"},
         "timemachine": {"enabled": True, "max_gb": 500}},
        {"name": "scans", "location": "system", "security": "public", "browseable": False}]]
    for s in specs:
        os.makedirs(S.share_path(s), exist_ok=True)
    (state / "shares.json").write_text(json.dumps({"shares": [{**s, "path": S.share_path(s)} for s in specs]}))
    return PreviewNas(), specs


# --- the dashboard ------------------------------------------------------------------------------------
class PreviewHost:
    """A host whose load drifts gently, and its last hour of samples."""

    @staticmethod
    def at(t):
        import math
        return {"cpu": 33 + 9 * math.sin(t / 300) + 4 * math.sin(t / 37), "memory": (21.5 + 1.5 * math.sin(t / 900)) * 1024 ** 3,
                "rx": max(0.0, 2.4e6 + 2e6 * math.sin(t / 120)), "tx": max(0.0, 3e5 + 2.5e5 * math.sin(t / 90))}

    def sample(self, now):
        v = self.at(time.time())
        return {"cpu": v["cpu"], "cores": [v["cpu"]] * 16, "threads": 16, "load": [3.84, 3.2, 2.9],
                "memory": {"total": 64 * 1024 ** 3, "used": v["memory"], "available": 64 * 1024 ** 3 - v["memory"]},
                "swap": {"total": 8 * 1024 ** 3, "used": 0}, "net": {"iface": "enp5s0", "rx": v["rx"], "tx": v["tx"]},
                "temperature": 47.0}

    def fill(self, history):
        now = time.time()
        for i in range(1800):
            t = now - (1800 - i) * 2
            v = self.at(t)
            for key, value in (("t", t), ("cpu", v["cpu"]), ("memory", v["memory"]), ("net_rx", v["rx"]), ("net_tx", v["tx"])):
                history[key].append(value)


def start_dockerd(path, docker):
    """A FakeDockerd in its own thread, streaming steady stats for the running containers."""
    shares = {"demo-web": (3.1e9, 900 * 2 ** 20), "monitor": (0.4e9, 210 * 2 ** 20)}
    ready = threading.Event()

    async def main():
        d = await FakeDockerd(path, interval=1.0).start()
        for c in docker.items:
            cpu, mem = shares.get(c["project"], (0.2e9, 60 * 2 ** 20))
            d.stats[c["id"]] = [docker_stats(total=1e10 + cpu, system=2e12 + 32e9, pre_total=1e10, pre_system=2e12, online=16,
                                             usage=mem + 50 * 2 ** 20, inactive=50 * 2 ** 20)]
        ready.set()
        await asyncio.Event().wait()
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    ready.wait(5)


# --- the two modes -----------------------------------------------------------------------------------
def preview(out, pages):
    tmp = pathlib.Path(tempfile.mkdtemp())
    www = prepare_www(tmp)
    state, docker = build_state(tmp, FIXTURE_CATALOG)
    isos, lv = build_vms(tmp)
    nas, specs = build_nas(tmp, state)
    (tmp / "tokens").mkdir()
    settings = Settings(run_dir=str(tmp), www_dir=str(www), os_release=str(tmp / "os-release"), state_dir=str(state),
                        catalog_dir=str(FIXTURE_CATALOG), logo_path=str(CC.parent / "packages/nimbusos-branding/art/logo.svg"),
                        isos_dir=str(isos), vnc_tokens_dir=str(tmp / "tokens"), docker_socket=str(tmp / "docker.sock"))
    privd = FakePrivd({"update.history": HISTORY})
    nas.install(privd)
    start_dockerd(tmp / "docker.sock", docker)
    # Another program holds dns's admin port: why its container exited, and
    # what the Dashboard's Fix finds and highlights (ui-dashboard-fix.png).
    held = socket.create_server(("127.0.0.1", 8081))  # noqa: F841 (open until the screenshots are done)
    host = PreviewHost()
    app = create_app(settings, privd=privd, docker=docker, libvirt=lv, bridges=lambda: ["br0"], host_sampler=host)
    host.fill(app.app.state.metrics.history)
    asyncio.run(app.app.state.metrics.refresh_storage())          # what the hourly refresh keeps
    usage = app.app.state.shares.usage
    for s, used in zip(specs, (2.9e12, 41e9, 380e9, 212e9, 1.2e9)):
        usage.cache[S.share_path(s)] = (time.monotonic(), int(used), True)
    server = serve(app)
    codes = app.app.state.codes
    for page in pages:
        # The Dashboard is live: its numbers arrive over a WebSocket in real seconds.
        take = (lambda p, u, **kw: live_shot(p, u, wait=6, **kw)) if page == "dashboard" else shot
        take(out / f"ui-{page}.png", page_url(codes, page))
        take(out / f"ui-{page}-full.png", page_url(codes, page), height=1800)
        take(out / f"ui-{page}-800.png", page_url(codes, page), width=800)
    for name, (page, clicks) in SCENES.items():
        if page in pages:
            shot(out / f"ui-{name}.png", page_url(codes, page, clicks), height=1000 if "create" in name else 900)
    if "apps" in pages:
        shot(out / "ui-apps-advanced-800.png", page_url(codes, "apps", SCENES["apps-advanced"][1]), width=800, height=1000)
    if "vms" in pages:
        shot(out / "ui-vms-create-800.png", page_url(codes, "vms", SCENES["vms-create"][1]), width=800, height=1000)
    if "storage" in pages:
        shot(out / "ui-storage-share-800.png", page_url(codes, "storage", SCENES["storage-share"][1]), width=800, height=1100)
        (state / "shares.json").write_text(json.dumps({"shares": []}))          # a NAS not set up yet: the wizard
        shot(out / "ui-storage-empty.png", page_url(codes, "storage"))
        shot(out / "ui-storage-wizard.png", page_url(codes, "storage", ["Set up NAS@0", "/dev/sdd*", "/dev/sde*"]), height=1000)
        shot(out / "ui-storage-wizard-mode.png", page_url(codes, "storage", ["Set up NAS@0", "/dev/sdd*", "/dev/sde*", "Next",
                                                                               "Pooled + parity*"]), height=1000)
        shot(out / "ui-storage-wizard-confirm.png", page_url(codes, "storage", [
            "Set up NAS@0", "/dev/sdd*", "/dev/sde*", "Next", "Pooled + parity*", "Next", "Next", "Next"]), height=1000)
    shot(out / "ui-signed-out.png", "http://127.0.0.1:7777/", width=900, height=500)
    server.should_exit = True


def live_vm(out):
    from nimbus_control.libvirt_api import LibvirtApi
    tmp = pathlib.Path(tempfile.mkdtemp())
    www = prepare_www(tmp)
    tokens = tmp / "tokens"
    tokens.mkdir()
    (tmp / "state").mkdir()
    ws = subprocess.Popen(["websockify", "--token-plugin=TokenFile", f"--token-source={tokens}", "127.0.0.1:6080"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    settings = Settings(run_dir=str(tmp), www_dir=str(www), os_release=str(tmp / "os-release"), state_dir=str(tmp / "state"),
                        catalog_dir=str(FIXTURE_CATALOG), logo_path=str(CC.parent / "packages/nimbusos-branding/art/logo.svg"),
                        vnc_tokens_dir=str(tokens))
    app = create_app(settings, privd=FakePrivd(), docker=FakeDocker(), libvirt=LibvirtApi())
    svc = app.app.state.vms
    with open(pathlib.Path(settings.isos_dir) / "blank.iso", "wb") as f:
        f.write(b"\0" * 1024 * 1024)
    result = asyncio.run(svc.create({"name": "uefi-demo", "preset": "linux", "vcpus": 1, "memory_mib": 512, "disk_gib": 1,
                                     "iso": "blank.iso", "network": {"mode": "nat"}}))
    print(result["message"])
    server = serve(app)
    try:
        time.sleep(4)                                 # let the firmware draw something
        codes = app.app.state.codes
        live_shot(out / "ui-vms-live.png", page_url(codes, "vms"), wait=8)
        live_shot(out / "ui-vms-console-live.png", page_url(codes, "vms", ["Console"]), wait=14)
    finally:
        server.should_exit = True
        asyncio.run(svc.delete("uefi-demo", True))
        ws.terminate()


def main(argv):
    if len(argv) > 2 and argv[1] == "--live-vm":
        out = pathlib.Path(argv[2])
        out.mkdir(parents=True, exist_ok=True)
        live_vm(out)
        return 0
    out = pathlib.Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    preview(out, argv[2:] or ["dashboard", "updates", "apps", "vms", "storage", "settings"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
