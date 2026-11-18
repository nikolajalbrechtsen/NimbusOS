# =============================================================================
# control-center/tests/ui_preview.py: screenshots of the Control Center UI
# =============================================================================
# What: serves the real frontend and backend on 127.0.0.1:7777 with FakePrivd
#       (canned update status and history), FakeDocker (four installed demo
#       apps: running, error, starting and stopped; one with a template
#       update) and the fixture catalog, then takes screenshots with headless
#       Chromium:
#         - each page given on the command line at 1400x900 and at full height
#           (1400x1800), and at 800 px (the narrow layout);
#         - "scenes": a page plus buttons to press, in order (by their text,
#           "@N" for the Nth match): the Apps catalog, Advanced install with
#           its Compose tab, the template diff;
#         - the signed-out page.
#       Animations are off (reduced motion), so nothing is caught mid-way.
#       The frontend is copied to a temporary folder with preview-actions.js
#       added before app.js: it reads ?click= before app.js removes the query
#       and presses the buttons once they appear. Each screenshot signs in
#       with its own one-time launch code, as the launcher does.
# Why:  the design system is checked by looking at it (spec "UI design
#       system"); used by developers and CI, never installed.
#
# Usage: python3 control-center/tests/ui_preview.py OUT_DIR [PAGE ...]
#        Needs chromium, python3-uvicorn, fonts-inter, fonts-jetbrains-mono.
# =============================================================================

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from conftest import CC, FIXTURE_CATALOG, FakeDocker, FakePrivd, container  # noqa: E402
from nimbus_control import compose  # noqa: E402
from nimbus_control.app import create_app  # noqa: E402
from nimbus_control.catalog import Catalog  # noqa: E402
from nimbus_control.config import Settings  # noqa: E402

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
}

ACTIONS_JS = """// preview-actions.js: presses buttons for ui_preview.py screenshots (never installed).
const wanted = (new URLSearchParams(location.search).get("click") || "").split("|").filter(Boolean);
async function press(spec) {
  const [text, nth] = spec.split("@");
  for (let i = 0; i < 100; i += 1) {
    const matches = [...document.querySelectorAll("button")].filter((b) => b.textContent.trim() === text);
    if (matches[Number(nth || 0)]) { matches[Number(nth || 0)].click(); return; }
    await new Promise((r) => setTimeout(r, 50));
  }
}
(async () => { for (const spec of wanted) { await press(spec); await new Promise((r) => setTimeout(r, 300)); } })();
"""


def build_state(tmp, catalog_dir):
    """Four installed demo apps, their Compose files, and one share."""
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


def main(argv):
    out = pathlib.Path(argv[1])
    pages = argv[2:] or ["updates", "apps"]
    out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "os-release").write_text('NAME="NimbusOS"\nVERSION="0.1.0 (stable)"\nVERSION_ID="0.1.0"\n'
                                    "NIMBUSOS_CHANNEL=stable\nNIMBUSOS_DEB_VERSION=\"0.1.0\"\n")
    www = tmp / "www"
    shutil.copytree(CC / "frontend", www)
    (www / "preview-actions.js").write_text(ACTIONS_JS)
    index = (www / "index.html").read_text()
    (www / "index.html").write_text(index.replace('<script type="module" src="/app.js">',
                                                  '<script type="module" src="/preview-actions.js"></script>\n  '
                                                  '<script type="module" src="/app.js">'))
    state, docker = build_state(tmp, FIXTURE_CATALOG)
    settings = Settings(run_dir=str(tmp), www_dir=str(www), os_release=str(tmp / "os-release"), state_dir=str(state),
                        catalog_dir=str(FIXTURE_CATALOG), logo_path=str(CC.parent / "packages/nimbusos-branding/art/logo.svg"))
    app = create_app(settings, privd=FakePrivd({"update.history": HISTORY}), docker=docker)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=7777, log_level="warning", ws="websockets"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:7777/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    codes = app.app.state.codes

    def shot(name, page=None, clicks=(), width=1400, height=900):
        path = out / f"{name}.png"
        query = {"launch": codes.issue()} if page else {}
        if clicks:
            query["click"] = "|".join(clicks)
        url = "http://127.0.0.1:7777/" + (f"?{urllib.parse.urlencode(query)}#{page}" if page else "")
        subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-prefers-reduced-motion",
                        f"--window-size={width},{height}", "--virtual-time-budget=8000", f"--screenshot={path}", url],
                       check=True, capture_output=True, timeout=120)
        print(f"wrote {path}")

    for page in pages:
        shot(f"ui-{page}", page)
        shot(f"ui-{page}-full", page, height=1800)
        shot(f"ui-{page}-800", page, width=800)
    for name, (page, clicks) in SCENES.items():
        if page in pages:
            shot(f"ui-{name}", page, clicks)
    if "apps" in pages:
        shot("ui-apps-advanced-800", "apps", SCENES["apps-advanced"][1], width=800, height=1000)
    shot("ui-signed-out", width=900, height=500)
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
