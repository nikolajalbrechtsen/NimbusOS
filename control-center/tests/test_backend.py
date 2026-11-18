# =============================================================================
# control-center/tests/test_backend.py: tests for the backend core
# =============================================================================
# What: the real app (create_app) with FakePrivd and a temporary run directory:
#         - the token file is 0640, made once and reused on the next start;
#         - Host other than 127.0.0.1/localhost:7777 is refused (DNS
#           rebinding); a foreign Origin is refused, the own one accepted;
#         - /api/* needs the token; static files and fonts don't; every
#           response has the CSP and related headers; API responses no-store;
#         - launch codes: need the token, work once, expire after 30 seconds;
#         - WebSockets need both subprotocols and the right Origin;
#         - the Updates API: status, check, channel and policy (bad values
#           rejected with the field named, before privd), install and job,
#           history limits, the log stream, privd errors mapped to statuses;
#         - the summary counts pending updates for the sidebar badge.
# Why:  spec section 3 (Security) and docs/PLAN.md §4.3, NOTE-14.
# =============================================================================

import os
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import CC, STATUS, FakePrivd
from nimbus_control.app import create_app
from nimbus_control.auth import LaunchCodes
from nimbus_control.config import Settings
from nimbus_control.privd_client import PrivdError

ORIGIN = "http://127.0.0.1:7777"
# A full URL: with a bare path the test client would send "Host: testserver",
# which the middleware rightly refuses, whatever else the test checks.
WS_URL = "ws://127.0.0.1:7777/ws/updates/0123456789ab"


@pytest.fixture
def env(tmp_path):
    os_release = tmp_path / "os-release"
    os_release.write_text('NAME="NimbusOS"\nVERSION="0.1.0 (stable)"\nVERSION_ID="0.1.0"\n'
                          "NIMBUSOS_CHANNEL=stable\nNIMBUSOS_DEB_VERSION=\"0.1.0\"\n")
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Inter-Regular.otf").write_bytes(b"OTTO-fake")
    settings = Settings(run_dir=str(tmp_path), www_dir=str(CC / "frontend"), os_release=str(os_release),
                        logo_path=str(CC.parent / "packages/nimbusos-branding/art/logo.svg"))
    return settings, str(fonts)


def make(env, privd=None):
    settings, fonts = env
    lister = lambda package: [os.path.join(fonts, n) for n in os.listdir(fonts)] if package == "fonts-inter" else []
    app = create_app(settings, privd=privd or FakePrivd(), font_lister=lister)
    return app, TestClient(app, base_url="http://127.0.0.1:7777")


def auth(app):
    return {"Authorization": f"Bearer {app.tokens.token}"}


def test_token_file(env, tmp_path):
    app, _ = make(env)
    path = tmp_path / "token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_text().strip() == app.tokens.token and len(app.tokens.token) == 43
    again, _ = make(env)
    assert again.tokens.token == app.tokens.token


def test_static_is_public_and_has_security_headers(env):
    app, client = make(env)
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'self'" in csp and "script-src 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    for path in ("/app.js", "/style.css", "/lib/ui.js", "/views/updates.js", "/logo.svg", "/fonts/inter-400"):
        assert client.get(path).status_code == 200, path
    assert client.get("/fonts/inter-400").headers["content-type"] == "font/otf"
    assert client.get("/fonts/mono-400").status_code == 404


def test_api_needs_the_token(env):
    app, client = make(env)
    r = client.get("/api/system/info")
    assert r.status_code == 401 and "app menu" in r.json()["message"]
    assert client.get("/api/system/info", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
    r = client.get("/api/system/info", headers=auth(app))
    assert r.status_code == 200 and r.json()["version"] == "0.1.0" and r.json()["channel"] == "stable"
    assert r.headers["cache-control"] == "no-store"


def test_host_and_origin(env):
    app, client = make(env)
    assert client.get("/api/system/info", headers={**auth(app), "host": "attacker.example:7777"}).status_code == 403
    assert client.get("/", headers={"host": "nimbusos.local:7777"}).status_code == 403
    assert client.get("/api/system/info", headers={**auth(app), "host": "localhost:7777"}).status_code == 200
    assert client.get("/api/system/info", headers={**auth(app), "origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/system/info", headers={**auth(app), "origin": ORIGIN}).status_code == 200


def test_launch_codes(env):
    app, client = make(env)
    assert client.post("/api/session/launch-code").status_code == 401
    code = client.post("/api/session/launch-code", headers=auth(app)).json()["code"]
    r = client.post("/api/session/exchange", json={"code": code})
    assert r.status_code == 200 and r.json()["token"] == app.tokens.token
    again = client.post("/api/session/exchange", json={"code": code})
    assert again.status_code == 403 and "expired or was already used" in again.json()["message"]
    assert client.post("/api/session/exchange", json={"code": "guess"}).status_code == 403


def test_launch_codes_expire():
    now = [1000.0]
    codes = LaunchCodes(clock=lambda: now[0])
    first, second = codes.issue(), codes.issue()
    now[0] += 29
    assert codes.redeem(first)
    now[0] += 2
    assert not codes.redeem(second)


def test_updates_api(env):
    privd = FakePrivd()
    app, client = make(env, privd)
    h = auth(app)
    assert client.get("/api/updates", headers=h).json()["counts"] == STATUS["counts"]
    assert client.post("/api/updates/check", headers=h).status_code == 200
    r = client.put("/api/updates/channel", json={"channel": "nightly"}, headers=h)
    assert r.status_code == 422 and r.json()["field"] == "channel"
    assert client.put("/api/updates/channel", json={"channel": "beta"}, headers=h).status_code == 200
    assert client.put("/api/updates/policy", json={"policy": "never"}, headers=h).status_code == 422
    assert client.put("/api/updates/policy", json={"policy": "notify"}, headers=h).status_code == 200
    assert client.get("/api/updates/history?limit=0", headers=h).status_code == 422
    assert client.get("/api/updates/history?limit=20", headers=h).status_code == 200
    assert client.post("/api/updates/install", headers=h).json() == {"job": "0123456789ab"}
    assert client.get("/api/updates/job", headers=h).json() == {"job": "0123456789ab"}
    verbs = [verb for verb, _ in privd.calls]
    assert "update.set_channel" in verbs and ("update.set_channel", {"channel": "nightly"}) not in privd.calls
    assert ("update.history", {"limit": 20}) in privd.calls


@pytest.mark.parametrize("code, status", [("busy", 409), ("failed", 502), ("unavailable", 503), ("invalid_argument", 422)])
def test_privd_errors_map_to_statuses(env, code, status):
    app, client = make(env, FakePrivd({"update.check": PrivdError(code, f"privd says {code}")}))
    r = client.post("/api/updates/check", headers=auth(app))
    assert r.status_code == status and r.json()["message"] == f"privd says {code}"


def test_update_log_websocket(env):
    privd = FakePrivd(results={"update.follow": {"job": "0123456789ab", "ok": True, "exit_status": 0}},
                      streams={"update.follow": ["==> apt-get update", "==> apt-get full-upgrade"]})
    app, client = make(env, privd)
    protocols = ["nimbus.v1", f"nimbus.token.{app.tokens.token}"]
    with client.websocket_connect(WS_URL, subprotocols=protocols, headers={"origin": ORIGIN}) as ws:
        assert ws.accepted_subprotocol == "nimbus.v1"
        messages = [ws.receive_json() for _ in range(3)]
    assert messages[0] == {"type": "log", "line": "==> apt-get update"}
    assert messages[2] == {"type": "done", "job": "0123456789ab", "ok": True, "exit_status": 0}


@pytest.mark.parametrize("protocols, origin", [
    (["nimbus.v1"], ORIGIN),                                   # no token
    (["nimbus.v1", "nimbus.token." + "x" * 43], ORIGIN),        # wrong token
    (None, ORIGIN),                                            # nothing offered
    ("good", "http://evil.example"),                           # foreign origin
    ("good", None),                                            # no origin at all
])
def test_websocket_refusals(env, protocols, origin):
    app, client = make(env)
    if protocols == "good":
        protocols = ["nimbus.v1", f"nimbus.token.{app.tokens.token}"]
    headers = {"origin": origin} if origin else {}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS_URL, subprotocols=protocols or [], headers=headers) as ws:
            ws.receive_json()


def test_summary_counts_updates(env):
    app, client = make(env)
    s = client.get("/api/system/summary", headers=auth(app)).json()
    assert s["counts"]["updates"] == 2
    assert s["memory"]["total"] > 0 and s["storage"]["total"] > 0 and s["version"] == "0.1.0"
