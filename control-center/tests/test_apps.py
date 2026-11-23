# =============================================================================
# control-center/tests/test_apps.py: the Apps API end to end
# =============================================================================
# What: the real app (create_app) with the fixture catalog, a temporary state
#       folder with one share, FakeDocker, FakePrivd and the fake docker
#       program (conftest.py):
#         - catalog and icons (icons public, nothing else is);
#         - 1-click plans: free ports (Docker, listening sockets, other
#           apps), LAN binding, fixed ports never moved, share_hint, generated
#           secrets, unique names;
#         - every invalid input refused with its field, before anything runs;
#         - install, actions, edit (settings and hand-edited Compose), the
#           template update with its diff, uninstall with and without data,
#           each as a job with a streamed log and the exact docker commands;
#         - a failed pull leaves the app listed; a rejected Compose file
#           leaves nothing behind;
#         - status (running, starting, stopped, error with the reason, Docker
#           down), web UI links, live logs over WebSocket;
#         - one job per app at a time; job errors in plain words;
#         - diagnose (the dashboard's Fix): a port another program holds, a
#           folder that isn't there, else the reason.
# Why:  spec Tab A; docs/PLAN.md NOTE-18, NOTE-29, NOTE-30, NOTE-31, NOTE-60.
# =============================================================================

import asyncio
import collections
import json
import shutil
import socket
import stat
import types

import psutil
import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import CC, FIXTURE_CATALOG, FakeDocker, FakePrivd, container
from nimbus_control import apps_service
from nimbus_control.app import create_app
from nimbus_control.apps_service import AppsService
from nimbus_control.config import Settings
from nimbus_control.errors import ApiError
from nimbus_control.jobs import JobFailed, JobManager
from nimbus_control.privd_client import PrivdError

ORIGIN = "http://127.0.0.1:7777"
WS = "ws://127.0.0.1:7777"
LAN = "192.168.1.20"
Addr = collections.namedtuple("Addr", "ip port")
Conn = collections.namedtuple("Conn", "laddr type status")


@pytest.fixture
def w(tmp_path, monkeypatch, docker_cli):
    catalog = tmp_path / "catalog"
    shutil.copytree(FIXTURE_CATALOG, catalog)
    state = tmp_path / "state"
    (state / "stacks").mkdir(parents=True)
    media = tmp_path / "mnt" / "Media"
    media.mkdir(parents=True)
    (state / "shares.json").write_text(json.dumps({"shares": [{"name": "Media", "path": str(media)}, {"broken": True}]}))
    listening = []
    monkeypatch.setattr(apps_service.psutil, "net_connections", lambda kind="inet": list(listening))
    monkeypatch.setattr(apps_service, "default_route_ip", lambda: LAN)
    docker, privd = FakeDocker(), FakePrivd()
    settings = Settings(run_dir=str(tmp_path), state_dir=str(state), catalog_dir=str(catalog), www_dir=str(CC / "frontend"))
    app = create_app(settings, privd=privd, font_lister=lambda package: [], docker=docker)
    # "with": one event loop for the whole test, so jobs keep running between requests.
    with TestClient(app, base_url=ORIGIN) as client:
        yield types.SimpleNamespace(app=app, client=client, h={"Authorization": f"Bearer {app.tokens.token}"},
                                    docker=docker, privd=privd, cli=docker_cli, catalog=catalog, state=state,
                                    stacks=state / "stacks", media=media, listening=listening,
                                    svc=app.app.state.apps)


def protocols(w):
    return ["nimbus.v1", f"nimbus.token.{w.app.tokens.token}"]


def finish(w, job):
    """Follow a job's WebSocket to the end: (done message, log lines)."""
    lines = []
    with w.client.websocket_connect(f"{WS}/ws/jobs/{job}", subprotocols=protocols(w), headers={"origin": ORIGIN}) as ws:
        while True:
            message = ws.receive_json()
            if message["type"] == "done":
                return message, lines
            lines.append(message["line"])


def install(w, app="demo-web", **body):
    r = w.client.post("/api/apps", json={"app": app, **body}, headers=w.h)
    assert r.status_code == 200, r.text
    done, lines = finish(w, r.json()["job"])
    return r.json()["name"], done, lines


def plan(w, app="demo-web", **body):
    r = w.client.post("/api/apps/plan", json={"app": app, **body}, headers=w.h)
    assert r.status_code == 200, r.text
    return r.json()


def records(w):
    return json.loads((w.state / "stacks.json").read_text())["stacks"]


def compose_cmd(w, name, *args):
    return " ".join(["compose", "--ansi", "never", "--progress", "plain", "--project-name", name,
                     "--file", str(w.stacks / name / "docker-compose.yml"), *args])


# --- catalog -------------------------------------------------------------------------
def test_catalog_and_public_icons(w):
    assert w.client.get("/api/catalog").status_code == 401
    apps = w.client.get("/api/catalog", headers=w.h).json()["apps"]
    assert [a["id"] for a in apps] == ["demo-dns", "demo-host", "demo-web"]          # by name
    web = apps[2]
    assert web == {"id": "demo-web", "name": "Demo Web", "description": web["description"], "category": "Media",
                   "notes": "Test fixture for control-center/tests; never packaged.", "template_version": 2,
                   "icon": "/catalog-icons/demo-web.svg", "installed_as": [], "webui": True}
    assert apps[1]["icon"] is None
    icon = w.client.get("/catalog-icons/demo-web.svg")          # no token: an <img> can't send one
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml") and b"<svg" in icon.content
    for path in ("/catalog-icons/demo-host.svg", "/catalog-icons/nope.svg", "/catalog-icons/Demo-Web.svg",
                 "/catalog-icons/..%2F..%2Fetc%2Fpasswd.svg"):
        assert w.client.get(path).status_code == 404, path


def test_apps_api_needs_the_token(w):
    for method, path in (("get", "/api/apps"), ("post", "/api/apps"), ("post", "/api/apps/plan"),
                         ("get", "/api/apps/options"), ("delete", "/api/apps/x"), ("get", "/api/jobs/0123456789ab")):
        assert getattr(w.client, method)(path).status_code == 401, path
    with pytest.raises(WebSocketDisconnect):
        with w.client.websocket_connect(f"{WS}/ws/apps/x/logs", subprotocols=["nimbus.v1"], headers={"origin": ORIGIN}) as ws:
            ws.receive_json()


def test_options(w):
    r = w.client.get("/api/apps/options", headers=w.h).json()
    assert r["shares"] == [{"name": "Media", "path": str(w.media)}]
    assert r["networks"] == ["proxy"] and r["lan_ip"] == LAN and r["hostname"] == socket.gethostname()
    w.docker.available = False
    assert w.client.get("/api/apps/options", headers=w.h).json()["networks"] == []


# --- planning ----------------------------------------------------------------------------
def test_one_click_plan(w):
    w.docker.items.append(container("other", "proxy", ports=[("tcp", 8080)]))
    r = plan(w)
    inputs = r["inputs"]
    assert inputs["name"] == "demo-web" and inputs["restart"] == "unless-stopped" and inputs["network_mode"] == "bridge"
    assert inputs["ports"] == [{"id": "http", "container": 80, "host": 8081, "protocol": "tcp", "ip": ""}]
    assert inputs["volumes"] == [
        {"id": "config", "container": "/config", "source": str(w.stacks / "demo-web" / "config"), "read_only": False},
        {"id": "media", "container": "/media", "source": str(w.media), "read_only": False}]          # share_hint
    assert inputs["env"]["TZ"] == "Etc/UTC" and len(inputs["env"]["ADMIN_PASSWORD"]) == 32
    assert plan(w)["inputs"]["env"]["ADMIN_PASSWORD"] != inputs["env"]["ADMIN_PASSWORD"]
    assert r["path"] == str(w.stacks / "demo-web") and r["existing_data"] is False and r["warnings"] == []
    assert "8081:80/tcp" in r["compose"] and r["notes"].startswith("Test fixture")
    assert r["manifest"]["env"][1]["secret"] is True and set(r["manifest"]) == {"name", "ports", "volumes", "env",
                                                                                "network_mode", "webui"}


def test_plan_without_the_share_uses_the_stack_folder(w):
    (w.state / "shares.json").write_text(json.dumps({"shares": []}))
    assert plan(w)["inputs"]["volumes"][1]["source"] == str(w.stacks / "demo-web" / "media")


def test_lan_and_fixed_ports(w):
    w.listening.append(Conn(Addr("0.0.0.0", 53), socket.SOCK_DGRAM, psutil.CONN_NONE))
    w.listening.append(Conn(Addr("0.0.0.0", 8081), socket.SOCK_STREAM, psutil.CONN_LISTEN))
    w.listening.append(Conn(Addr("0.0.0.0", 53), socket.SOCK_STREAM, psutil.CONN_ESTABLISHED))   # not listening
    r = plan(w, "demo-dns")
    ports = r["inputs"]["ports"]
    assert [(p["host"], p["protocol"], p["ip"]) for p in ports] == [(53, "udp", LAN), (53, "tcp", LAN), (8082, "tcp", "")]
    assert r["warnings"] == ["Port 53/udp is used by another program on this computer, and Demo DNS needs exactly this port."]
    assert len(r["inputs"]["env"]["WEBPASSWORD"]) == 64
    assert r["inputs"]["volumes"][0]["source"] == "dnsdata"


def test_lan_binding_without_a_lan_address(w, monkeypatch):
    monkeypatch.setattr(apps_service, "default_route_ip", lambda: None)
    r = plan(w, "demo-dns")
    assert r["inputs"]["ports"][0]["ip"] == ""
    assert sum("no LAN address was found" in x for x in r["warnings"]) == 2


def test_host_network_plan_has_no_ports(w):
    r = plan(w, "demo-host")
    assert r["inputs"]["ports"] == [] and r["inputs"]["network_mode"] == "host"
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in r["compose"]


def test_unknown_app_and_bad_names(w):
    r = w.client.post("/api/apps/plan", json={"app": "nope"}, headers=w.h)
    assert r.status_code == 404 and r.json()["field"] == "app"
    r = w.client.post("/api/apps/plan", json={"app": "demo-web", "name": "../etc"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "name"


# --- validation ------------------------------------------------------------------------------
INVALID = [
    (lambda i: i.update(name="My App"), "name", "lowercase letters"),
    (lambda i: i.update(name="a" * 49), "name", "at most 48"),
    (lambda i: i["ports"][0].update(host=0), "ports.0.host", "1 to 65535"),
    (lambda i: i["ports"][0].update(container="80"), "ports.0.container", "1 to 65535"),
    (lambda i: i["ports"][0].update(protocol="sctp"), "ports.0.protocol", "Must be one of: tcp, udp"),
    (lambda i: i["ports"][0].update(ip="300.1.1.1"), "ports.0.ip", "not an IPv4 address"),
    (lambda i: i["ports"].append(dict(i["ports"][0], id=None, container=81)), "ports.1.host", "mapped twice"),
    (lambda i: i.update(ports="8080"), "ports", "At most 32"),
    (lambda i: i["volumes"][0].update(source="/etc"), "volumes.0.source", "outside this app's folder"),
    (lambda i: i["volumes"][0].update(source="/var/lib/nimbus-control/stacks/other/x"), "volumes.0.source", "outside"),
    (lambda i: i["volumes"][0].update(source="data/../../x"), "volumes.0.source", "'.' and '..'"),
    (lambda i: i["volumes"][0].update(source=" "), "volumes.0.source", "Choose a folder or a share"),
    (lambda i: i["volumes"][0].update(source="bad volume"), "volumes.0.source", "folder path or a Docker volume"),
    (lambda i: i["volumes"][0].update(container="config"), "volumes.0.container", "absolute"),
    (lambda i: i["volumes"][0].update(container="/a:/b"), "volumes.0.container", "absolute"),
    (lambda i: i["env"].update({"1BAD": "x"}), "env.1BAD.name", "Variable names"),
    (lambda i: i["env"].update(ADMIN_PASSWORD=""), "env.ADMIN_PASSWORD", "ADMIN_PASSWORD is required"),
    (lambda i: i["env"].update(TZ="a\x00b"), "env.TZ", "must be text"),
    (lambda i: i["env"].update(TZ=5), "env.TZ", "must be text"),
    (lambda i: i.update(env=["TZ=x"]), "env", "name and value pairs"),
    (lambda i: i.update(restart="sometimes"), "restart", "Must be one of"),
    (lambda i: i.update(network_mode="bad net"), "network_mode", "Network names"),
]


@pytest.mark.parametrize("change, field, text", INVALID)
def test_invalid_inputs_are_refused_with_the_field(w, change, field, text):
    inputs = plan(w)["inputs"]
    change(inputs)
    r = w.client.post("/api/apps", json={"app": "demo-web", "inputs": inputs}, headers=w.h)
    assert r.status_code == 422, r.text
    assert r.json()["field"] == field and text in r.json()["message"]
    assert w.cli.calls() == [] and not (w.stacks / "demo-web").exists()


def test_symlink_out_of_the_stack_folder_is_refused(w):
    (w.stacks / "demo-web").mkdir()
    (w.stacks / "demo-web" / "escape").symlink_to("/etc")
    inputs = plan(w)["inputs"]
    inputs["volumes"][0]["source"] = str(w.stacks / "demo-web" / "escape")
    r = w.client.post("/api/apps", json={"app": "demo-web", "inputs": inputs}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "volumes.0.source"


def test_ports_held_by_other_apps(w):
    install(w)
    second = plan(w)
    assert second["inputs"]["name"] == "demo-web-2" and second["inputs"]["ports"][0]["host"] == 8081
    inputs = second["inputs"]
    inputs["ports"][0]["host"] = 8080
    r = w.client.post("/api/apps", json={"app": "demo-web", "inputs": inputs}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "ports.0.host"
    assert r.json()["message"] == "Port 8080/tcp is used by the app 'demo-web'. Use 8081 instead."
    inputs["name"] = "demo-web"
    r = w.client.post("/api/apps", json={"app": "demo-web", "inputs": inputs}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "name" and "already installed" in r.json()["message"]


def test_fixed_port_cannot_move(w):
    inputs = plan(w, "demo-dns")["inputs"]
    inputs["ports"][0]["host"] = 5353
    r = w.client.post("/api/apps", json={"app": "demo-dns", "inputs": inputs}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "ports.0.host"
    assert r.json()["message"] == "Demo DNS needs host port 53 for this mapping."


def test_one_click_with_a_taken_fixed_port_names_the_field(w):
    w.listening.append(Conn(Addr("0.0.0.0", 53), socket.SOCK_DGRAM, psutil.CONN_NONE))
    r = w.client.post("/api/apps", json={"app": "demo-dns"}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "ports.0.host"
    assert "Port 53/udp is used by another program" in r.json()["message"] and "Use" not in r.json()["message"]


# --- install and status ------------------------------------------------------------------------
def test_one_click_install(w):
    name, done, lines = install(w)
    assert name == "demo-web"
    assert done == {"type": "done", "ok": True, "message": "Demo Web is installed as 'demo-web'."}
    stack = w.stacks / "demo-web"
    calls = w.cli.calls()
    assert calls[0] == (f"compose --project-name demo-web --project-directory {stack} "
                        f"--file {stack}/.docker-compose.check.yml config --quiet")
    assert calls[1:] == [compose_cmd(w, "demo-web", "pull"), compose_cmd(w, "demo-web", "up", "--detach", "--remove-orphans")]
    assert ("stack.prepare_volume", {"stack": "demo-web", "path": "config", "uid": 472, "gid": 472}) in w.privd.calls
    assert f"$ docker {compose_cmd(w, 'demo-web', 'pull')}" in lines and " web Pulled" in lines
    assert lines.index(f"Saved to {stack}/docker-compose.yml") < lines.index(f"$ docker {compose_cmd(w, 'demo-web', 'pull')}")

    file = stack / "docker-compose.yml"
    assert stat.S_IMODE(file.stat().st_mode) == 0o640
    doc = yaml.safe_load(file.read_text())
    assert doc["name"] == "demo-web"
    web = doc["services"]["web"]
    assert web["ports"] == ["8080:80/tcp"] and web["restart"] == "unless-stopped"
    assert web["volumes"] == [f"{stack}/config:/config", f"{w.media}:/media"]
    record = records(w)["demo-web"]
    assert record["app"] == "demo-web" and record["template_version"] == 2 and record["desired"] == "running"
    assert record["custom"] is False and record["inputs"]["env"]["ADMIN_PASSWORD"] == web["environment"]["ADMIN_PASSWORD"]
    assert w.client.get("/api/catalog", headers=w.h).json()["apps"][2]["installed_as"] == ["demo-web"]
    assert w.client.get("/api/system/summary", headers=w.h).json()["counts"]["apps"] == 1


def test_status_and_web_links(w):
    install(w)
    install(w, "demo-host")
    w.docker.items[:] = [container("demo-web", "web", ports=[("tcp", 8080)]), container("demo-host", "monitor")]
    apps = {a["name"]: a for a in w.client.get("/api/apps", headers=w.h).json()["apps"]}
    web = apps["demo-web"]
    assert web["state"] == "running" and web["reason"] == "" and web["busy"] is None and web["job"] is None
    assert web["webui"] == f"http://{socket.gethostname()}:8080/" and web["icon"] == "/catalog-icons/demo-web.svg"
    assert web["ports"][0]["host"] == 8080 and web["path"] == str(w.stacks / "demo-web") and web["template_update"] is False
    host = apps["demo-host"]
    assert host["webui"] == "http://localhost:9000/" and host["network_mode"] == "host" and host["icon"] is None

    w.docker.items[0] = container("demo-web", "web", state="exited", exit_code=1)
    web = w.client.get("/api/apps", headers=w.h).json()["apps"][1]
    assert (web["state"], web["reason"]) == ("error", "web exited unexpectedly (exit code 1).")

    w.docker.available = False
    r = w.client.get("/api/apps", headers=w.h).json()
    assert r["docker"]["ok"] is False and "sudo systemctl start docker" in r["docker"]["message"]
    assert {a["state"] for a in r["apps"]} == {"unknown"}


C = dict(service="web", state="running", exit_code=0, health=None, oom_killed=False)


@pytest.mark.parametrize("containers, desired, expected", [
    ([], "running", ("error", "Its containers are missing. Start it to create them again.")),
    ([], "stopped", ("stopped", "Stopped.")),
    ([C], "running", ("running", "")),
    ([C], "stopped", ("running", "")),
    ([{**C, "health": "starting"}], "running", ("starting", "web is starting.")),
    ([{**C, "health": "unhealthy"}], "running", ("error", "web is unhealthy.")),
    ([{**C, "state": "restarting"}], "stopped", ("error", "web keeps restarting.")),
    ([{**C, "state": "exited", "exit_code": 137, "oom_killed": True}], "running", ("error", "web ran out of memory and stopped.")),
    ([{**C, "state": "exited", "exit_code": 1}], "running", ("error", "web exited unexpectedly (exit code 1).")),
    ([{**C, "state": "exited", "exit_code": 0}], "running", ("stopped", "Stopped.")),
    ([{**C, "state": "exited", "exit_code": 137}], "stopped", ("stopped", "Stopped.")),
    ([C, {**C, "service": "db", "state": "dead", "exit_code": 2}], "running", ("error", "db exited unexpectedly (exit code 2).")),
])
def test_stack_status(containers, desired, expected):
    assert AppsService.stack_status(containers, desired) == expected


def test_failed_pull_keeps_the_app_listed(w):
    w.cli.fail("pull")
    name, done, lines = install(w)
    assert done["ok"] is False and done["message"] == "docker failed (exit status 1); the log above says why."
    assert "Error response from daemon: pull failed" in lines
    assert "demo-web" in records(w)
    app = w.client.get("/api/apps", headers=w.h).json()["apps"][0]
    assert app["name"] == "demo-web" and app["state"] == "error" and "Start it" in app["reason"]


def test_rejected_compose_leaves_nothing_behind(w):
    w.cli.config_error("services.web.image must be a string")
    name, done, _ = install(w)
    assert done["ok"] is False and done["message"] == "The Compose file is not valid: services.web.image must be a string"
    assert not (w.stacks / "demo-web").exists() and not (w.state / "stacks.json").exists()
    assert w.client.get("/api/apps", headers=w.h).json()["apps"] == [] and w.svc.pending == {}
    assert len(w.cli.calls()) == 1


def test_install_in_progress_is_listed_and_reserved(w):
    inputs = plan(w)["inputs"]
    w.svc.pending["demo-web"] = {"app": "demo-web", "inputs": inputs}
    assert w.svc.unique_name("demo-web") == "demo-web-2"
    assert plan(w)["inputs"]["ports"][0]["host"] == 8081
    app = w.client.get("/api/apps", headers=w.h).json()["apps"][0]
    assert app["name"] == "demo-web" and app["state"] == "unknown" and app["template_update"] is False
    r = w.client.post("/api/apps", json={"app": "demo-web", "inputs": inputs}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "name"


# --- actions, edit, template update, uninstall ---------------------------------------------------
def test_actions_record_what_the_user_wants(w):
    install(w)
    before = len(w.cli.calls())
    for action, desired, args in (("stop", "stopped", [("stop",)]), ("start", "running", [("up", "--detach")]),
                                  ("restart", "running", [("restart",)]),
                                  ("update", "running", [("pull",), ("up", "--detach", "--remove-orphans")])):
        r = w.client.post(f"/api/apps/demo-web/actions/{action}", headers=w.h)
        assert r.status_code == 200 and r.json()["name"] == "demo-web"
        done, _ = finish(w, r.json()["job"])
        assert done["ok"] is True
        assert records(w)["demo-web"]["desired"] == desired
        assert w.cli.calls()[before:] == [compose_cmd(w, "demo-web", *a) for a in args]
        before = len(w.cli.calls())
    assert w.client.post("/api/apps/demo-web/actions/explode", headers=w.h).status_code == 404
    r = w.client.post("/api/apps/nope/actions/start", headers=w.h)
    assert r.status_code == 404 and "no installed app named 'nope'" in r.json()["message"]


def test_edit_with_settings_and_with_a_hand_edited_file(w):
    install(w)
    current = w.client.get("/api/apps/demo-web", headers=w.h).json()
    assert current["record"]["app"] == "demo-web" and "services:" in current["compose"]
    inputs = current["record"]["inputs"]
    inputs["ports"][0]["host"] = 8090                         # its own old port is not "taken"
    r = w.client.put("/api/apps/demo-web", json={"inputs": inputs}, headers=w.h)
    assert r.status_code == 200
    assert finish(w, r.json()["job"])[0]["message"] == "'demo-web' was updated with the new settings."
    assert "8090:80/tcp" in (w.stacks / "demo-web" / "docker-compose.yml").read_text()

    bad = "services:\n  web:\n    image: docker.io/library/nginx:1.27\n    volumes: ['/etc:/host-etc']\n"
    r = w.client.put("/api/apps/demo-web", json={"inputs": inputs, "compose": bad}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "compose" and "/etc" in r.json()["message"]

    custom = "services:\n  web:\n    image: docker.io/library/nginx:1.27\n    ports: ['8091:80']\n"
    r = w.client.put("/api/apps/demo-web", json={"inputs": inputs, "compose": custom}, headers=w.h)
    assert finish(w, r.json()["job"])[0]["ok"] is True
    assert (w.stacks / "demo-web" / "docker-compose.yml").read_text() == custom
    assert records(w)["demo-web"]["custom"] is True
    assert w.client.get("/api/apps", headers=w.h).json()["apps"][0]["custom"] is True
    assert w.client.put("/api/apps/nope", json={}, headers=w.h).status_code == 404


def test_preview_and_validate(w):
    inputs = plan(w)["inputs"]
    inputs["ports"][0]["host"] = 9999
    r = w.client.post("/api/apps/preview", json={"app": "demo-web", "inputs": inputs}, headers=w.h)
    assert r.status_code == 200 and "9999:80/tcp" in r.json()["compose"]
    good = "services:\n  web:\n    image: x\n    volumes: ['./data:/data']\n"
    r = w.client.post("/api/apps/validate", json={"app": "demo-web", "name": "demo-web", "compose": good}, headers=w.h)
    assert r.json() == {"ok": True, "services": ["web"]}
    assert plan(w)["existing_data"] is False                   # the check left no folder behind
    w.cli.config_error("services.web.image must be a string")
    r = w.client.post("/api/apps/validate", json={"app": "demo-web", "name": "demo-web", "compose": good}, headers=w.h)
    assert r.status_code == 422 and r.json() == {"message": "docker compose config: services.web.image must be a string",
                                                 "field": "compose"}
    r = w.client.post("/api/apps/validate", json={"app": "demo-web", "name": "demo-web", "compose": "services: ["}, headers=w.h)
    assert r.status_code == 422 and r.json()["field"] == "compose" and "not valid YAML" in r.json()["message"]


def test_template_update_shows_the_diff_first(w):
    install(w)
    manifest = json.loads((w.catalog / "demo-web" / "manifest.json").read_text())
    manifest["template_version"] = 3
    (w.catalog / "demo-web" / "manifest.json").write_text(json.dumps(manifest, indent=4))
    template = (w.catalog / "demo-web" / "docker-compose.yml.j2").read_text()
    (w.catalog / "demo-web" / "docker-compose.yml.j2").write_text(template + "    labels:\n      fixture.version: \"3\"\n")
    before = (w.stacks / "demo-web" / "docker-compose.yml").read_text()

    app = w.client.get("/api/apps", headers=w.h).json()["apps"][0]
    assert app["template_update"] is True and app["template_version"] == 2
    diff = w.client.get("/api/apps/demo-web/template-diff", headers=w.h).json()
    assert diff["from"] == 2 and diff["to"] == 3 and diff["custom"] is False
    assert "+    labels:" in diff["diff"] and "+      fixture.version: '3'" in diff["diff"]
    assert not any(line.startswith("-") and not line.startswith("---") for line in diff["diff"]), "|".join(diff["diff"])
    password = records(w)["demo-web"]["inputs"]["env"]["ADMIN_PASSWORD"]
    assert "       ADMIN_PASSWORD: ••••••••" in diff["diff"] and password not in json.dumps(diff)
    # Looking changes nothing.
    assert (w.stacks / "demo-web" / "docker-compose.yml").read_text() == before
    assert records(w)["demo-web"]["template_version"] == 2

    r = w.client.post("/api/apps/demo-web/template-update", headers=w.h)
    assert finish(w, r.json()["job"])[0]["message"] == "'demo-web' now uses template version 3."
    assert records(w)["demo-web"]["template_version"] == 3
    assert "fixture.version" in (w.stacks / "demo-web" / "docker-compose.yml").read_text()
    assert w.client.get("/api/apps", headers=w.h).json()["apps"][0]["template_update"] is False


def test_secret_values_are_masked_in_diffs():
    manifest = {"env": [{"name": "DB_PASSWORD", "secret": True}, {"name": "TZ", "secret": False}, {"name": "PIN", "secret": True}]}
    inputs = {"env": {"DB_PASSWORD": "s3cr$t-value", "TZ": "Europe/Copenhagen", "PIN": "1234"}}
    lines = ["+      DB_PASSWORD: s3cr$$t-value", "-      DATABASE_URL: postgres://app:s3cr$$t-value@db/app",
             "       TZ: Europe/Copenhagen", "+      PIN: '1234'", "@@ -1,2 +1,2 @@ port 1234"]
    assert AppsService._mask_secrets(lines, manifest, inputs) == [
        "+      DB_PASSWORD: ••••••••", "-      DATABASE_URL: postgres://app:••••••••@db/app",
        "       TZ: Europe/Copenhagen", "+      PIN: ••••••••", "@@ -1,2 +1,2 @@ port 1234"]


def test_uninstall_keeps_data_unless_asked(w):
    install(w)
    settings = w.stacks / "demo-web" / "config" / "settings.xml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("<keep/>")
    before = len(w.cli.calls())
    r = w.client.delete("/api/apps/demo-web", headers=w.h)
    done, lines = finish(w, r.json()["job"])
    assert done["message"] == f"'demo-web' was removed. Its data is kept in {w.stacks / 'demo-web'}."
    assert w.cli.calls()[before:] == [compose_cmd(w, "demo-web", "down", "--remove-orphans")]
    assert settings.read_text() == "<keep/>" and "demo-web" not in records(w)
    assert not any(verb == "stack.delete_data" for verb, _ in w.privd.calls)
    assert w.client.get("/api/apps", headers=w.h).json()["apps"] == []

    assert plan(w)["existing_data"] is True                     # installing again finds the data
    install(w)
    before = len(w.cli.calls())
    r = w.client.delete("/api/apps/demo-web?delete_data=true", headers=w.h)
    assert finish(w, r.json()["job"])[0]["message"] == "'demo-web' was removed."
    assert w.cli.calls()[before:] == [compose_cmd(w, "demo-web", "down", "--remove-orphans", "--volumes")]
    assert ("stack.delete_data", {"stack": "demo-web"}) in w.privd.calls
    assert w.client.delete("/api/apps/demo-web", headers=w.h).status_code == 404


def test_uninstall_reports_privd_errors_plainly(w):
    install(w)
    w.privd.results["stack.delete_data"] = PrivdError("invalid_argument", "/x is not a plain folder; nothing was deleted.")
    r = w.client.delete("/api/apps/demo-web?delete_data=true", headers=w.h)
    done, _ = finish(w, r.json()["job"])
    assert done == {"type": "done", "ok": False, "message": "/x is not a plain folder; nothing was deleted."}
    assert "demo-web" in records(w)                              # still listed: nothing half-removed is hidden


# --- logs and jobs -------------------------------------------------------------------------------
def test_live_logs(w):
    install(w)
    with w.client.websocket_connect(f"{WS}/ws/apps/demo-web/logs", subprotocols=protocols(w), headers={"origin": ORIGIN}) as ws:
        assert ws.accepted_subprotocol == "nimbus.v1"
        messages = [ws.receive_json() for _ in range(3)]
    assert messages[:2] == [{"type": "log", "line": "web-1  | hello"}, {"type": "log", "line": "web-1  | world"}]
    assert messages[2]["type"] == "done"
    assert w.cli.calls()[-1] == compose_cmd(w, "demo-web", "logs", "--follow", "--tail", "200", "--no-color")
    with pytest.raises(WebSocketDisconnect) as err:
        with w.client.websocket_connect(f"{WS}/ws/apps/nope/logs", subprotocols=protocols(w), headers={"origin": ORIGIN}) as ws:
            ws.receive_json()
    assert err.value.code == 4404


def test_unknown_job(w):
    r = w.client.get("/api/jobs/0123456789ab", headers=w.h)
    assert r.status_code == 404 and "unknown" in r.json()["message"]
    with pytest.raises(WebSocketDisconnect):
        with w.client.websocket_connect(f"{WS}/ws/jobs/0123456789ab", subprotocols=protocols(w), headers={"origin": ORIGIN}) as ws:
            ws.receive_json()


def test_one_job_per_app_and_plain_errors():
    async def main():
        jobs = JobManager()
        gate = asyncio.Event()

        async def wait(job):
            job.log("waiting")
            await gate.wait()

        async def fails(job):
            raise JobFailed("The disk is full.")

        async def bug(job):
            raise KeyError("secret detail")

        async def relative(job):
            await job.run(["docker", "ps"])

        first = jobs.start("install", "web", wait, "Installed.")
        with pytest.raises(ApiError) as err:
            jobs.start("stop", "web", wait, "Stopped.")
        assert err.value.status == 409 and err.value.message == "'web' is busy (install). Wait until it has finished."
        others = [jobs.start("x", key, work, "ok") for key, work in (("a", fails), ("b", bug), ("c", relative))]
        assert jobs.running("web") is first
        gate.set()
        await asyncio.sleep(0.05)
        assert first.summary() == {"id": first.id, "kind": "install", "key": "web", "state": "ok", "message": "Installed."}
        assert [(j.state, j.message) for j in others] == [
            ("failed", "The disk is full."),
            ("failed", "Something went wrong inside the Control Center; see the log."),
            ("failed", "Something went wrong inside the Control Center; see the log.")]
        assert "secret detail" not in others[1].message and jobs.running("web") is None
        follow = [item async for item in first.follow()]
        assert follow[0] == ("log", "waiting") and follow[-1][0] == "done"
    asyncio.run(main())


# --- the dashboard's Fix ------------------------------------------------------------------------
def test_diagnose_names_the_setting(w):
    install(w)
    (w.stacks / "demo-web" / "config").mkdir(parents=True, exist_ok=True)
    w.listening.append(Conn(Addr("0.0.0.0", 8080), socket.SOCK_STREAM, psutil.CONN_LISTEN))     # it isn't running
    diagnose = lambda: w.client.get("/api/apps/demo-web/diagnose", headers=w.h).json()        # noqa: E731
    assert diagnose() == {"field": "ports.0.host",
                          "message": "Port 8080/tcp is used by another program on this computer. Use 8081 instead."}
    w.listening.clear()
    shutil.rmtree(w.media)
    found = diagnose()
    assert found["field"] == "volumes.1.source" and f"{w.media} isn't there" in found["message"]
    w.media.mkdir()
    assert diagnose() == {"field": None, "message": "Its containers are missing. Start it to create them again. "
                                                    "Its log (Logs) usually says why."}
    w.docker.items[:] = [container("demo-web", "web", ports=[("tcp", 8080)])]
    w.listening.append(Conn(Addr("0.0.0.0", 8080), socket.SOCK_STREAM, psutil.CONN_LISTEN))     # its own port now
    assert diagnose()["field"] is None
    assert w.client.get("/api/apps/nope/diagnose", headers=w.h).status_code == 404
