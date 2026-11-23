# =============================================================================
# control-center/tests/test_catalog.py: the real app catalog (deliverable 9)
# =============================================================================
# What: - catalog/ holds exactly the spec's ten apps, each passes
#         manifest.schema.json and the Control Center's parser, and the
#         Catalog loads them with no errors;
#       - the schema and parse_manifest accept the same manifests: one full
#         manifest and 81 changes to it, valid and broken, must get the same
#         verdict from both (plus the checks only the parser can make:
#         id = folder, unique ids, webui.port names a port);
#       - templates: a header comment, every value through "q", full image
#         names with the vendor's documented tag;
#       - icons: small, plain SVG (no scripts, links or external files);
#       - through the API, with the fake docker program: each app's 1-click
#         plan renders, its binds stay in allowed places, and all ten install
#         side by side on their default ports; the spec's quirks (Nextcloud
#         AIO's fixed names and socket, Home Assistant's host network,
#         Pi-hole's DNS on the LAN address, Jellyfin's media share,
#         Syncthing's backups share); the host's time zone and the shares'
#         group reach the templates;
#       - Nextcloud AIO: a second install is refused, its HTTPS link, its
#         nextcloud-aio-* containers don't change its status, and uninstall
#         stops and removes them (their volumes only with "delete data");
#       - where Docker's CLI and Compose plugin are installed
#         (tests/integration/catalog-images.sh), "docker compose config"
#         accepts every app's 1-click file.
# Why:  spec Tab A (the ten apps and their quirks) and deliverable 9;
#       catalog/SCHEMA.md; docs/PLAN.md NOTE-29, NOTE-30, NOTE-70.
# =============================================================================

import asyncio
import copy
import json
import re
import subprocess
import types
import xml.etree.ElementTree as ET

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import CC, FakeDocker, FakePrivd, container
from nimbus_control import apps_service, compose
from nimbus_control.app import create_app
from nimbus_control.catalog import Catalog
from nimbus_control.config import Settings
from nimbus_control.manifest import ManifestError, parse_manifest

CATALOG = CC.parent / "catalog"
SCHEMA = json.loads((CATALOG / "manifest.schema.json").read_text())
VALIDATOR = jsonschema.validators.validator_for(SCHEMA)(SCHEMA)
ORIGIN = "http://127.0.0.1:7777"
LAN = "192.168.1.20"
IMAGES = {
    "portainer": "docker.io/portainer/portainer-ce:lts",
    "uptime-kuma": "docker.io/louislam/uptime-kuma:2",
    "vaultwarden": "docker.io/vaultwarden/server:latest",
    "nextcloud-aio": "ghcr.io/nextcloud-releases/all-in-one:latest",
    "jellyfin": "docker.io/jellyfin/jellyfin:latest",
    "pihole": "docker.io/pihole/pihole:latest",
    "syncthing": "docker.io/syncthing/syncthing:latest",
    "home-assistant": "ghcr.io/home-assistant/home-assistant:stable",
    "grafana": "docker.io/grafana/grafana:latest",
    "nginx-proxy-manager": "docker.io/jc21/nginx-proxy-manager:latest",
}


def manifest_json(app):
    return json.loads((CATALOG / app / "manifest.json").read_text())


def template(app):
    return (CATALOG / app / "docker-compose.yml.j2").read_text()


# --- the catalog as shipped --------------------------------------------------------------
def test_the_ten_apps_load_cleanly():
    apps = sorted(p.name for p in CATALOG.iterdir() if p.is_dir())
    assert apps == sorted(IMAGES)
    catalog = Catalog(str(CATALOG))
    assert sorted(catalog.apps()) == sorted(IMAGES) and catalog.errors == []
    for app in apps:
        assert sorted(f.name for f in (CATALOG / app).iterdir()) == ["docker-compose.yml.j2", "icon.svg", "manifest.json"]
        data = manifest_json(app)
        assert list(VALIDATOR.iter_errors(data)) == [], app
        m = parse_manifest(data, app)
        assert m["template_version"] == 1 and m["notes"] and m["webui"], app


def test_templates_quote_every_value_and_use_documented_images():
    for app, image in IMAGES.items():
        text = template(app)
        assert text.startswith(f"{{# catalog/{app}/docker-compose.yml.j2: "), app
        for expr in re.findall(r"\{\{(.*?)\}\}", text, re.S):
            assert re.search(r"\|\s*q\s*$", expr), f"{app}: {{{{{expr}}}}} doesn't end in | q"
        assert "{%" not in text, app                          # no logic: the engine decides
        body = yaml.safe_load(re.sub(r"\{\{.*?\}\}", '"x"', re.sub(r"\{#.*?#\}", "", text, flags=re.S)))
        service = body["services"][manifest_json(app)["main_service"]]
        assert service["image"] == image, app
        assert not {"ports", "volumes", "restart", "privileged", "cap_add", "devices"} & set(service), app


def test_icons_are_small_plain_svg():
    for app in IMAGES:
        raw = (CATALOG / app / "icon.svg").read_text()
        assert len(raw) < 2048 and f"<!-- catalog/{app}/icon.svg:" in raw, app
        root = ET.fromstring(raw.encode())
        assert root.tag == "{http://www.w3.org/2000/svg}svg" and root.get("viewBox") == "0 0 64 64"
        for el in root.iter():
            assert el.tag.split("}")[1] in ("svg", "rect", "text"), (app, el.tag)
            assert not any("href" in k or k.startswith("on") for k in el.attrib), (app, el.attrib)
        assert root.get("aria-label") == manifest_json(app)["name"]


def test_default_host_ports_dont_collide():
    seen = {}
    for app in IMAGES:
        m = parse_manifest(manifest_json(app), app)
        for p in m["ports"]:
            key = (p["protocol"], p["host"])
            assert key not in seen, f"{app} and {seen.get(key)} both default to {key}"
            seen[key] = app
    assert not {("tcp", 7777), ("tcp", 6080)} & set(seen)          # the Control Center and websockify


# --- schema and parser agree ---------------------------------------------------------------
FULL = {
    "id": "full", "name": "Full", "description": "Every field.", "category": "Test", "template_version": 3,
    "main_service": "app", "network_mode": "bridge",
    "ports": [{"id": "web", "container": 80, "host": 8080, "protocol": "tcp", "bind": "all", "fixed": False, "description": "Web"},
              {"id": "dns", "container": 53, "protocol": "udp", "bind": "lan", "fixed": True}],
    "volumes": [{"id": "config", "container": "/config", "kind": "bind", "default": "config/sub",
                 "owner": {"uid": 472, "gid": 0}, "description": "Settings"},
                {"id": "media", "container": "/media", "share_hint": "media"},
                {"id": "db", "container": "/var/lib/db", "kind": "named", "default": "full_db"}],
    "env": [{"name": "TZ", "default": "Etc/UTC", "description": "Zone"},
            {"name": "PASSWORD", "default": None, "required": True, "secret": True, "generate": "hex"}],
    "host_paths": [{"host": "/run/dbus", "container": "/run/dbus", "read_only": True}],
    "webui": {"port": "web", "path": "/admin/", "scheme": "https", "secure_context": False},
    "single_instance": True,
    "companions": {"containers": "full-", "volumes": "full_", "network": "full-net"},
    "notes": "All of it.",
}


def setter(path, value):
    """A change to FULL: set (or with value=DELETE remove) the key at PATH."""
    def change(m):
        *parents, last = path
        for key in parents:
            m = m[key]
        if value is DELETE:
            del m[last]
        else:
            m[last] = value
    return change


DELETE = object()
CHANGES = [
    # valid
    ("only the required fields", lambda m: [m.pop(k) for k in list(m) if k not in
                                            ("id", "name", "description", "category", "template_version", "main_service")], True),
    ("no web page", setter(("webui",), None), True),
    ("host network", setter(("network_mode",), "host"), True),
    ("bind volume without default", setter(("volumes", 0, "default"), DELETE), True),
    ("port without host", setter(("ports", 0, "host"), DELETE), True),
    ("generate null", setter(("env", 0, "generate"), None), True),
    ("only a companion network", setter(("companions",), {"network": "x"}), True),
    ("owner without gid", setter(("volumes", 0, "owner"), {"uid": 0}), True),
    ("udp port", setter(("ports", 0, "protocol"), "udp"), True),
    ("deep relative folder", setter(("volumes", 0, "default"), "a/b/c/d/e/f/g/h"), True),
    # top level
    ("unknown key", setter(("icon",), "icon.svg"), False),
    ("upper-case id", setter(("id",), "Full"), False),
    ("blank name", setter(("name",), "   "), False),
    ("no description", setter(("description",), DELETE), False),
    ("category not a string", setter(("category",), 5), False),
    ("template_version as a string", setter(("template_version",), "3"), False),
    ("template_version 0", setter(("template_version",), 0), False),
    ("template_version true", setter(("template_version",), True), False),
    ("bad main_service", setter(("main_service",), "App"), False),
    ("network none", setter(("network_mode",), "none"), False),
    ("network null", setter(("network_mode",), None), False),
    ("ports not a list", setter(("ports",), {"web": {"id": "web", "container": 80}}), False),
    ("single_instance as a string", setter(("single_instance",), "yes"), False),
    ("notes not a string", setter(("notes",), 5), False),
    # ports
    ("port 0", setter(("ports", 0, "container"), 0), False),
    ("port 70000", setter(("ports", 0, "host"), 70000), False),
    ("host port as a string", setter(("ports", 0, "host"), "8080"), False),
    ("sctp", setter(("ports", 0, "protocol"), "sctp"), False),
    ("bind public", setter(("ports", 0, "bind"), "public"), False),
    ("port id with capitals", setter(("ports", 0, "id"), "Web"), False),
    ("fixed as a string", setter(("ports", 0, "fixed"), "yes"), False),
    ("port description a number", setter(("ports", 0, "description"), 5), False),
    ("port without container", setter(("ports", 0, "container"), DELETE), False),
    ("unknown port key", setter(("ports", 0, "ip"), "0.0.0.0"), False),
    # volumes
    ("relative container path", setter(("volumes", 0, "container"), "config"), False),
    ("colon in container path", setter(("volumes", 0, "container"), "/a:b"), False),
    ("dot-dot in container path", setter(("volumes", 0, "container"), "/a/../b"), False),
    ("tmpfs", setter(("volumes", 0, "kind"), "tmpfs"), False),
    ("folder leaving the app", setter(("volumes", 0, "default"), "../x"), False),
    ("dot in a folder", setter(("volumes", 0, "default"), "a/./b"), False),
    ("absolute folder", setter(("volumes", 0, "default"), "/srv"), False),
    ("folder nine deep", setter(("volumes", 0, "default"), "a/b/c/d/e/f/g/h/i"), False),
    ("named without default", setter(("volumes", 2, "default"), DELETE), False),
    ("named with a space", setter(("volumes", 2, "default"), "bad name"), False),
    ("named with share_hint", setter(("volumes", 2, "share_hint"), "media"), False),
    ("share_hint homes", setter(("volumes", 1, "share_hint"), "homes"), False),
    ("share_hint null", setter(("volumes", 1, "share_hint"), None), False),
    ("share_hint with a slash", setter(("volumes", 1, "share_hint"), "a/b"), False),
    ("misspelt share-hint", setter(("volumes", 1, "share-hint"), "media"), False),
    ("owner null", setter(("volumes", 0, "owner"), None), False),
    ("owner uid -1", setter(("volumes", 0, "owner"), {"uid": -1}), False),
    ("owner uid 70000", setter(("volumes", 0, "owner"), {"uid": 70000}), False),
    ("owner by name", setter(("volumes", 0, "owner"), {"user": "grafana"}), False),
    ("owner gid as a string", setter(("volumes", 0, "owner"), {"uid": 1, "gid": "0"}), False),
    ("volume id with a dash", setter(("volumes", 0, "id"), "my-config"), False),
    # env
    ("variable starting with a digit", setter(("env", 0, "name"), "1TZ"), False),
    ("variable without a name", setter(("env", 0, "name"), DELETE), False),
    ("default a number", setter(("env", 0, "default"), 5), False),
    ("generate uuid", setter(("env", 1, "generate"), "uuid"), False),
    ("secret as a string", setter(("env", 1, "secret"), "no"), False),
    ("required as a number", setter(("env", 1, "required"), 1), False),
    ("unknown env key", setter(("env", 0, "value"), "x"), False),
    # host paths
    ("relative host path", setter(("host_paths", 0, "host"), "run/dbus"), False),
    ("colon in host path", setter(("host_paths", 0, "host"), "/a:b"), False),
    ("dot-dot in host path", setter(("host_paths", 0, "host"), "/run/../etc"), False),
    ("host path without container", setter(("host_paths", 0, "container"), DELETE), False),
    ("read_only as a number", setter(("host_paths", 0, "read_only"), 1), False),
    # webui
    ("relative web path", setter(("webui", "path"), "admin"), False),
    ("space in web path", setter(("webui", "path"), "/a b"), False),
    ("query in web path", setter(("webui", "path"), "/a?x=1"), False),
    ("scheme ftp", setter(("webui", "scheme"), "ftp"), False),
    ("secure_context as a string", setter(("webui", "secure_context"), "true"), False),
    ("unknown webui key", setter(("webui", "url"), "http://x"), False),
    ("webui without port", setter(("webui", "port"), DELETE), False),
    # companions
    ("empty companions", setter(("companions",), {}), False),
    ("companions null", setter(("companions",), None), False),
    ("prefix too short", setter(("companions", "containers"), "ab-"), False),
    ("prefix without its dash", setter(("companions", "containers"), "full"), False),
    ("prefix null", setter(("companions", "volumes"), None), False),
    ("network with a space", setter(("companions", "network"), "bad net"), False),
    ("unknown companions key", setter(("companions", "images"), "full-"), False),
]


def verdicts(data, folder="full"):
    schema_ok = VALIDATOR.is_valid(data)
    try:
        parse_manifest(data, folder)
        parser_ok = True
    except ManifestError:
        parser_ok = False
    return schema_ok, parser_ok


def test_the_full_manifest_is_valid_everywhere():
    assert verdicts(FULL) == (True, True)
    m = parse_manifest(copy.deepcopy(FULL), "full")
    assert m["webui"] == {"port": "web", "path": "/admin/", "scheme": "https", "secure_context": False}
    assert m["single_instance"] is True and m["companions"] == {"containers": "full-", "volumes": "full_", "network": "full-net"}
    assert m["volumes"][0]["owner"] == {"uid": 472, "gid": 0} and m["ports"][1]["host"] == 53


@pytest.mark.parametrize("name, change, valid", CHANGES, ids=[c[0] for c in CHANGES])
def test_schema_and_parser_agree(name, change, valid):
    data = copy.deepcopy(FULL)
    change(data)
    assert verdicts(data) == (valid, valid)


@pytest.mark.parametrize("name, change", [
    ("id isn't the folder name", setter(("id",), "other")),
    ("port id used twice", lambda m: m["ports"].append({"id": "web", "container": 81})),
    ("volume id used twice", lambda m: m["volumes"].append({"id": "config", "container": "/x"})),
    ("variable listed twice", lambda m: m["env"].append({"name": "TZ"})),
    ("webui.port names no port", setter(("webui", "port"), "nope")),
    ("template_version 3.0", setter(("template_version",), 3.0)),
])
def test_what_only_the_parser_can_check(name, change):
    # A JSON Schema can't compare a value with the folder name or with other
    # entries, and counts 3.0 as an integer; the parser (and check-catalog.py
    # for template_version) catches these.
    data = copy.deepcopy(FULL)
    change(data)
    assert verdicts(data) == (True, False)


# --- through the API ------------------------------------------------------------------------
@pytest.fixture
def w(tmp_path, monkeypatch, docker_cli):
    state = tmp_path / "state"
    (state / "stacks").mkdir(parents=True)
    shares = {name: tmp_path / "mnt" / name for name in ("media", "backups")}
    for path in shares.values():
        path.mkdir(parents=True)
    (state / "shares.json").write_text(json.dumps({"shares": [{"name": n, "path": str(p)} for n, p in shares.items()]}))
    monkeypatch.setattr(apps_service.psutil, "net_connections", lambda kind="inet": [])
    monkeypatch.setattr(apps_service, "default_route_ip", lambda: LAN)
    monkeypatch.setattr(apps_service, "host_timezone", lambda: "Europe/Copenhagen")
    monkeypatch.setattr(apps_service, "share_gid", lambda: 1234)
    monkeypatch.setattr(apps_service.socket, "gethostname", lambda: "nimbus")
    docker, privd = FakeDocker(), FakePrivd()
    settings = Settings(run_dir=str(tmp_path), state_dir=str(state), catalog_dir=str(CATALOG), www_dir=str(CC / "frontend"))
    app = create_app(settings, privd=privd, font_lister=lambda package: [], docker=docker)
    with TestClient(app, base_url=ORIGIN) as client:
        yield types.SimpleNamespace(client=client, h={"Authorization": f"Bearer {app.tokens.token}"}, token=app.tokens.token,
                                    docker=docker, privd=privd, cli=docker_cli, state=state, stacks=state / "stacks",
                                    shares=shares, svc=app.app.state.apps)


def plan(w, app):
    r = w.client.post("/api/apps/plan", json={"app": app}, headers=w.h)
    assert r.status_code == 200, r.text
    return r.json()


def finish(w, job):
    with w.client.websocket_connect(f"ws://127.0.0.1:7777/ws/jobs/{job}", headers={"origin": ORIGIN},
                                    subprotocols=["nimbus.v1", f"nimbus.token.{w.token}"]) as ws:
        while True:
            message = ws.receive_json()
            if message["type"] == "done":
                return message


def install(w, app):
    r = w.client.post("/api/apps", json={"app": app}, headers=w.h)
    assert r.status_code == 200, r.text
    done = finish(w, r.json()["job"])
    assert done["ok"], done
    return r.json()["name"]


def uninstall(w, name, delete_data=False):
    r = w.client.delete(f"/api/apps/{name}" + ("?delete_data=true" if delete_data else ""), headers=w.h)
    assert r.status_code == 200, r.text
    return finish(w, r.json()["job"])


def test_every_app_plans_on_its_defaults(w):
    for app in IMAGES:
        m = parse_manifest(manifest_json(app), app)
        r = plan(w, app)
        inputs = r["inputs"]
        assert r["warnings"] == [] and inputs["name"] == app and inputs["restart"] == "unless-stopped", app
        if m["network_mode"] == "bridge":
            assert [(p["id"], p["host"]) for p in inputs["ports"]] == [(p["id"], p["host"]) for p in m["ports"]], app
        doc = yaml.safe_load(r["compose"])
        compose.check_binds(doc, str(w.stacks / app), [str(p) for p in w.shares.values()],
                            [hp["host"] for hp in m["host_paths"]])
        service = doc["services"][m["main_service"]]
        assert service["image"] == IMAGES[app] and doc["name"] == app and "privileged" not in service, app
        for e in m["env"]:
            if e["generate"]:
                assert len(service["environment"][e["name"]]) == 32, app
        for v in m["volumes"]:
            if v["kind"] == "bind" and not v["share_hint"]:
                assert f"{w.stacks / app / v['default']}:{v['container']}" in service["volumes"], app


def test_the_spec_quirks(w):
    aio = yaml.safe_load(plan(w, "nextcloud-aio")["compose"])
    master = aio["services"]["mastercontainer"]
    assert master["container_name"] == "nextcloud-aio-mastercontainer" and master["init"] is True
    assert master["network_mode"] == "bridge" and master["ports"] == ["8080:8080/tcp"]
    assert master["volumes"] == ["nextcloud_aio_mastercontainer:/mnt/docker-aio-config",
                                 "/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert aio["volumes"] == {"nextcloud_aio_mastercontainer": {"name": "nextcloud_aio_mastercontainer"}}

    ha = yaml.safe_load(plan(w, "home-assistant")["compose"])["services"]["homeassistant"]
    assert ha["network_mode"] == "host" and "ports" not in ha and ha["stop_grace_period"] == "60s"
    assert "/run/dbus:/run/dbus:ro" in ha["volumes"] and ha["environment"]["TZ"] == "Europe/Copenhagen"

    pihole = yaml.safe_load(plan(w, "pihole")["compose"])["services"]["pihole"]
    assert pihole["ports"] == [f"{LAN}:53:53/udp", f"{LAN}:53:53/tcp", "8053:80/tcp"]
    assert pihole["environment"]["FTLCONF_dns_listeningMode"] == "ALL" and pihole["environment"]["TZ"] == "Europe/Copenhagen"

    jellyfin = yaml.safe_load(plan(w, "jellyfin")["compose"])["services"]["jellyfin"]
    assert f"{w.shares['media']}:/media" in jellyfin["volumes"]

    syncthing = yaml.safe_load(plan(w, "syncthing")["compose"])["services"]["syncthing"]
    assert f"{w.shares['backups']}:/var/syncthing/Sync" in syncthing["volumes"]
    assert f"{w.stacks / 'syncthing' / 'config'}:/var/syncthing/config" in syncthing["volumes"]
    assert syncthing["environment"] == {"PUID": "1000", "PGID": "1234", "UMASK": "002"} and syncthing["hostname"] == "nimbus"


def test_all_ten_install_side_by_side(w):
    for app in IMAGES:
        assert install(w, app) == app
    listed = {a["name"]: a for a in w.client.get("/api/apps", headers=w.h).json()["apps"]}
    assert sorted(listed) == sorted(IMAGES)
    assert listed["nextcloud-aio"]["webui"] == "https://nimbus:8080/"
    assert listed["portainer"]["webui"] == "https://nimbus:9443/"
    assert listed["vaultwarden"]["webui"] == "http://localhost:8222/"
    assert listed["pihole"]["webui"] == "http://nimbus:8053/admin/"
    assert listed["home-assistant"]["webui"] == "http://nimbus:8123/"
    # Folders handed to the containers' users before their first start (NOTE-31).
    prepared = sorted((a["stack"], a["path"], a["uid"]) for verb, a in w.privd.calls if verb == "stack.prepare_volume")
    assert prepared == [("grafana", "data", 472), ("syncthing", "config", 1000)]      # Syncthing's data is the share
    # Only Nextcloud AIO is single_instance, and the catalog says it's taken.
    catalog = {a["id"]: a for a in w.client.get("/api/catalog", headers=w.h).json()["apps"]}
    assert catalog["nextcloud-aio"]["single_instance"] is True and catalog["nextcloud-aio"]["installed_as"] == ["nextcloud-aio"]
    assert [a for a, c in catalog.items() if c["single_instance"]] == ["nextcloud-aio"]


def test_nextcloud_aio_once_and_its_companions(w):
    install(w, "nextcloud-aio")
    r = w.client.post("/api/apps/plan", json={"app": "nextcloud-aio"}, headers=w.h)
    assert r.status_code == 409 and "already installed as 'nextcloud-aio'" in r.json()["message"]
    r = w.client.post("/api/apps", json={"app": "nextcloud-aio", "inputs": {"name": "cloud2"}}, headers=w.h)
    assert r.status_code == 409

    master = {**container("nextcloud-aio", "mastercontainer"), "name": "nextcloud-aio-mastercontainer"}
    apache = {**container("", "x", state="exited", exit_code=137), "id": "apache", "name": "nextcloud-aio-apache"}
    database = {**container("", "x"), "id": "database", "name": "nextcloud-aio-database"}
    stranger = {**container("", "x"), "id": "stranger", "name": "other-nextcloud-aio-x"}
    w.docker.items += [master, apache, database, stranger]
    w.docker.network_names.append("nextcloud-aio")
    w.docker.volume_names += ["nextcloud_aio_mastercontainer", "nextcloud_aio_nextcloud_data", "unrelated"]
    assert w.svc.companion_prefixes() == {"nextcloud-aio-": "nextcloud-aio"}
    # A companion that stopped (AIO stops them for its backups) isn't the app's error.
    row = next(a for a in w.client.get("/api/apps", headers=w.h).json()["apps"] if a["name"] == "nextcloud-aio")
    assert row["state"] == "running"

    before = len(w.cli.calls())
    done = uninstall(w, "nextcloud-aio")
    calls = w.cli.calls()[before:]
    assert calls[1:] == ["stop --time 60 nextcloud-aio-apache nextcloud-aio-database",
                         "rm nextcloud-aio-apache nextcloud-aio-database", "network rm nextcloud-aio"]
    assert done["ok"]

    install(w, "nextcloud-aio")                         # once it's gone it can be installed again
    before = len(w.cli.calls())
    uninstall(w, "nextcloud-aio", delete_data=True)
    assert w.cli.calls()[-1] == "volume rm nextcloud_aio_mastercontainer nextcloud_aio_nextcloud_data"


def test_host_timezone(tmp_path):
    (tmp_path / "timezone").write_text("Europe/Copenhagen\n")
    assert apps_service.host_timezone(str(tmp_path / "timezone")) == "Europe/Copenhagen"
    (tmp_path / "localtime").symlink_to("/usr/share/zoneinfo/America/Argentina/Buenos_Aires")
    assert apps_service.host_timezone(str(tmp_path / "none"), str(tmp_path / "localtime")) == "America/Argentina/Buenos_Aires"
    (tmp_path / "bad").write_text("../../etc/passwd\n")
    assert apps_service.host_timezone(str(tmp_path / "bad"), str(tmp_path / "none")) == "Etc/UTC"


# --- the real "docker compose config" ----------------------------------------------------------
def real_compose():
    try:
        return subprocess.run([compose.DOCKER, "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not real_compose(), reason="needs docker-ce-cli and docker-compose-plugin (catalog-images.sh)")
@pytest.mark.parametrize("app", sorted(IMAGES))
def test_real_docker_compose_accepts_every_app(tmp_path, app):
    m = parse_manifest(manifest_json(app), app)
    inputs = {"name": app, "network_mode": m["network_mode"], "restart": "unless-stopped",
              "ports": [] if m["network_mode"] == "host" else
              [{"id": p["id"], "container": p["container"], "host": p["host"], "protocol": p["protocol"],
                "ip": LAN if p["bind"] == "lan" else ""} for p in m["ports"]],
              "volumes": [{"id": v["id"], "container": v["container"], "read_only": False,
                           "source": v["default"] if v["kind"] == "named" else str(tmp_path / app / v["default"])}
                          for v in m["volumes"]],
              "env": {e["name"]: e["default"] or "s3cret$value" for e in m["env"]}}
    text, _ = compose.build(m, template(app), inputs, host_ip=LAN, hostname="nimbus", timezone="Europe/Copenhagen",
                            share_gid=1234)
    assert asyncio.run(compose.docker_config_check(text, str(tmp_path / app), app)) is None
