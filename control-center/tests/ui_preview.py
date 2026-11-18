# =============================================================================
# control-center/tests/ui_preview.py: screenshots of the Control Center UI
# =============================================================================
# What: serves the real frontend and backend on 127.0.0.1:7777 with FakePrivd
#       (canned update status and history) and Debian's fonts, then takes
#       screenshots with headless Chromium: each page given on the command
#       line at 1400x900 and at full height (1400x1800), the Updates page at
#       800 px (the narrow layout), and the signed-out page. Each screenshot
#       signs in with its own one-time launch code, as the launcher does.
# Why:  the design system is checked by looking at it (spec "UI design
#       system"); used by developers and CI, never installed.
#
# Usage: python3 control-center/tests/ui_preview.py OUT_DIR [PAGE ...]
#        Needs chromium, python3-uvicorn, fonts-inter, fonts-jetbrains-mono.
# =============================================================================

import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from conftest import CC, FakePrivd  # noqa: E402
from nimbus_control.app import create_app  # noqa: E402
from nimbus_control.config import Settings  # noqa: E402

HISTORY = {"entries": [
    {"start": "2026-11-17  06:25:12", "end": "2026-11-17  06:26:40", "command": "apt-get -y -q full-upgrade",
     "requested_by": "", "error": None, "changes": {"upgrade": [{"name": "openssl", "versions": ["3.5.1-1"], "automatic": False}] * 4}},
    {"start": "2026-11-15  06:12:03", "end": "2026-11-15  06:12:30", "command": "/usr/bin/unattended-upgrade",
     "requested_by": "", "error": None, "changes": {"upgrade": [{"name": "libc6", "versions": [], "automatic": False}]}},
]}


def main(argv):
    out = pathlib.Path(argv[1])
    pages = argv[2:] or ["updates"]
    out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "os-release").write_text('NAME="NimbusOS"\nVERSION="0.1.0 (stable)"\nVERSION_ID="0.1.0"\n'
                                    "NIMBUSOS_CHANNEL=stable\nNIMBUSOS_DEB_VERSION=\"0.1.0\"\n")
    settings = Settings(run_dir=str(tmp), www_dir=str(CC / "frontend"), os_release=str(tmp / "os-release"),
                        logo_path=str(CC.parent / "packages/nimbusos-branding/art/logo.svg"))
    app = create_app(settings, privd=FakePrivd({"update.history": HISTORY}))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=7777, log_level="warning", ws="websockets"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:7777/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    def shot(name, url, width=1400, height=900):
        path = out / f"{name}.png"
        subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        f"--window-size={width},{height}", "--virtual-time-budget=6000", f"--screenshot={path}", url],
                       check=True, capture_output=True, timeout=120)
        print(f"wrote {path}")

    codes = app.app.state.codes
    for page in pages:
        shot(f"ui-{page}", f"http://127.0.0.1:7777/?launch={codes.issue()}#{page}")
        shot(f"ui-{page}-full", f"http://127.0.0.1:7777/?launch={codes.issue()}#{page}", height=1800)
    shot("ui-updates-800", f"http://127.0.0.1:7777/?launch={codes.issue()}#updates", width=800, height=900)
    shot("ui-signed-out", "http://127.0.0.1:7777/", width=900, height=500)
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
