# =============================================================================
# control-center/tests/test_compose.py: the Compose engine
# =============================================================================
# What: - q: every value becomes one quoted YAML scalar with "$" doubled, so
#         nothing a user types can add keys (privileged, extra volumes) or
#         make Compose interpolate variables;
#       - render: the template sandbox (no Python internals), missing values
#         are errors;
#       - build: the engine sets the main service's ports (LAN address when
#         given), volumes, host paths, environment, restart, network mode
#         and the project name; named volumes stay project-scoped;
#       - check_binds (NOTE-30): the stack folder, shares and declared host
#         paths are allowed; /etc, ~, ".." and links out of the stack folder
#         are refused, in every service and in both volume syntaxes;
#       - parse rejects non-YAML and files without services;
#       - docker_config_check runs "docker compose config", reports its
#         message, and leaves no folder behind for an app never installed;
#       - where Docker's CLI and Compose plugin are installed, the real
#         "docker compose config" accepts what the engine writes for every
#         fixture app and reads each value back exactly as typed.
# Why:  spec Tab A (deploy engine, Advanced install) and docs/PLAN.md NOTE-30.
# =============================================================================

import asyncio
import json
import subprocess

import pytest
import yaml

from conftest import FIXTURE_CATALOG
from nimbus_control import compose
from nimbus_control.manifest import parse_manifest


def manifest(app):
    return parse_manifest(json.loads((FIXTURE_CATALOG / app / "manifest.json").read_text()), app)


def template(app):
    return (FIXTURE_CATALOG / app / "docker-compose.yml.j2").read_text()


def web_inputs(**changes):
    inputs = {"name": "web1", "ports": [{"id": "http", "container": 80, "host": 8080, "protocol": "tcp", "ip": ""}],
              "volumes": [{"id": "config", "container": "/config", "source": "/stacks/web1/config", "read_only": False},
                          {"id": "media", "container": "/media", "source": "/srv/media", "read_only": True}],
              "env": {"TZ": "Europe/Copenhagen", "ADMIN_PASSWORD": "pa$$word"},
              "network_mode": "bridge", "restart": "unless-stopped"}
    inputs.update(changes)
    return inputs


NASTY = 'x"\n    privileged: true\n    volumes: ["/:/host"]\n#'


def test_q_makes_one_scalar_and_escapes_dollars():
    assert compose.q("a$b") == '"a$$b"'
    assert compose.q(8080) == "8080" and compose.q(None) == "null" and compose.q(True) == "true"
    assert yaml.safe_load("key: " + compose.q(NASTY)) == {"key": NASTY}
    tpl = "services:\n  web:\n    image: x\n    environment:\n      SITE: {{ value | q }}\n"
    doc = yaml.safe_load(compose.render(tpl, {"value": NASTY}))
    assert doc == {"services": {"web": {"image": "x", "environment": {"SITE": NASTY}}}}


def test_render_is_sandboxed_and_strict():
    with pytest.raises(compose.ComposeError, match="could not be filled in"):
        compose.render("{{ missing }}", {})
    with pytest.raises(compose.ComposeError):
        compose.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})
    with pytest.raises(compose.ComposeError):
        compose.render("{{ cycler.__init__.__globals__ }}", {})


def test_build_configures_the_main_service():
    text, doc = compose.build(manifest("demo-web"), template("demo-web"), web_inputs(), host_ip="192.168.1.20",
                              hostname="nimbus")
    assert yaml.safe_load(text) == doc
    assert doc["name"] == "web1"
    web = doc["services"]["web"]
    assert web["image"] == "docker.io/library/nginx:1.27"
    assert web["ports"] == ["8080:80/tcp"]
    assert web["volumes"] == ["/stacks/web1/config:/config", "/srv/media:/media:ro"]
    assert web["restart"] == "unless-stopped" and "network_mode" not in web
    # Template values first, then the install inputs with "$" escaped for Compose.
    assert web["environment"] == {"SITE_NAME": "web1", "PUBLIC_URL": "http://nimbus:8080", "TZ": "Europe/Copenhagen",
                                  "ADMIN_PASSWORD": "pa$$$$word"}


def test_user_values_cannot_change_the_service():
    _, doc = compose.build(manifest("demo-web"), template("demo-web"), web_inputs(env={"TZ": NASTY, "ADMIN_PASSWORD": "x"}))
    web = doc["services"]["web"]
    assert "privileged" not in web and web["environment"]["TZ"] == NASTY
    assert web["volumes"] == ["/stacks/web1/config:/config", "/srv/media:/media:ro"]


def test_lan_ports_networks_and_named_volumes():
    inputs = {"name": "dns", "ports": [{"id": "dns_udp", "container": 53, "host": 53, "protocol": "udp", "ip": "192.168.1.20"}],
              "volumes": [{"id": "data", "container": "/etc/dns", "source": "dnsdata", "read_only": False}],
              "env": {}, "network_mode": "proxy", "restart": "always"}
    _, doc = compose.build(manifest("demo-dns"), template("demo-dns"), inputs, host_ip="192.168.1.20")
    dns = doc["services"]["dns"]
    assert dns["ports"] == ["192.168.1.20:53:53/udp"]
    assert dns["networks"] == ["proxy"] and doc["networks"] == {"proxy": {"external": True}}
    # No explicit "name": Docker scopes it to the project (dns_dnsdata).
    assert dns["volumes"] == ["dnsdata:/etc/dns"] and doc["volumes"] == {"dnsdata": {}}
    assert dns["environment"] == {"LISTEN_ADDRESS": "192.168.1.20"}


def test_a_template_can_pin_a_volume_name():
    inputs = {"name": "aio", "ports": [], "env": {}, "network_mode": "bridge", "restart": "always",
              "volumes": [{"id": "data", "container": "/mnt/docker-aio-config", "source": "nextcloud_aio_mastercontainer",
                           "read_only": False}]}
    tpl = ("services:\n  dns:\n    image: x\n"
           "volumes:\n  nextcloud_aio_mastercontainer:\n    name: nextcloud_aio_mastercontainer\n")
    _, doc = compose.build(manifest("demo-dns"), tpl, inputs)
    assert doc["volumes"] == {"nextcloud_aio_mastercontainer": {"name": "nextcloud_aio_mastercontainer"}}
    assert doc["services"]["dns"]["volumes"] == ["nextcloud_aio_mastercontainer:/mnt/docker-aio-config"]


def test_host_network_drops_ports_and_adds_host_paths():
    inputs = {"name": "mon", "ports": [{"id": "ui", "container": 9000, "host": 9000, "protocol": "tcp", "ip": ""}],
              "volumes": [], "env": {}, "network_mode": "host", "restart": "always"}
    _, doc = compose.build(manifest("demo-host"), template("demo-host"), inputs)
    mon = doc["services"]["monitor"]
    assert mon["network_mode"] == "host" and "ports" not in mon and "networks" not in mon
    assert mon["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert "environment" not in mon


def test_template_must_have_the_main_service():
    with pytest.raises(compose.ComposeError, match="no service 'web'"):
        compose.build(manifest("demo-web"), "services:\n  other:\n    image: x\n", web_inputs())
    with pytest.raises(compose.ComposeError, match="invalid YAML"):
        compose.build(manifest("demo-web"), "services: [\n", web_inputs())


# --- bind mounts (NOTE-30) ---------------------------------------------------------------
@pytest.fixture
def places(tmp_path):
    stack = tmp_path / "stacks" / "web1"
    stack.mkdir(parents=True)
    share = tmp_path / "mnt" / "Media"
    share.mkdir(parents=True)
    (stack / "escape").symlink_to("/etc")
    return {"stack": str(stack), "share": str(share)}


def fill(entry, places):
    if isinstance(entry, dict):
        return {k: v.format(**places) for k, v in entry.items()}
    return entry.format(**places)


def check(places, *entries, service="web"):
    doc = {"services": {"web": {"image": "x"}, service: {"image": "x", "volumes": [fill(e, places) for e in entries]}}}
    compose.check_binds(doc, places["stack"], [places["share"]], ["/var/run/docker.sock"])


@pytest.mark.parametrize("entry", [
    "./data:/data", "data:/named-volume", "{stack}/config:/config", "{stack}:/all", "{share}/films:/films:ro",
    "/var/run/docker.sock:/var/run/docker.sock:ro",
    {"type": "bind", "source": "{stack}/x", "target": "/x"}, {"type": "volume", "source": "db", "target": "/db"},
])
def test_allowed_binds(places, entry):
    check(places, entry)


@pytest.mark.parametrize("entry, text", [
    ("/etc:/host-etc", "outside this app's folder"),
    ("/:/host", "outside this app's folder"),
    ("~/:/home", "home folders"),
    ("../other:/other", "outside this app's folder"),
    ("{stack}/../other:/other", "outside this app's folder"),
    ("{stack}/escape/passwd:/p", "outside this app's folder"),
    ("{share}-evil:/x", "outside this app's folder"),
    ("/var/run/docker.sock.d:/x", "outside this app's folder"),
    ({"type": "bind", "source": "/", "target": "/host"}, "outside this app's folder"),
])
def test_refused_binds(places, entry, text):
    with pytest.raises(compose.ComposeError) as err:
        check(places, entry)
    assert text in err.value.message and err.value.field == "compose"


def test_every_service_is_checked(places):
    with pytest.raises(compose.ComposeError, match="Service 'db'"):
        check(places, "/etc:/etc", service="db")
    with pytest.raises(compose.ComposeError, match="not a mapping"):
        compose.check_binds({"services": {"web": "nginx"}}, places["stack"], [], [])


@pytest.mark.parametrize("text", ["services: [\n", "just text", "services: {}\n", "version: '3'\n"])
def test_parse_refuses_non_compose(text):
    with pytest.raises(compose.ComposeError) as err:
        compose.parse(text)
    assert err.value.field == "compose"


# --- docker compose config ------------------------------------------------------------------
def test_docker_config_check(tmp_path, docker_cli):
    stack = tmp_path / "stacks" / "web1"
    assert asyncio.run(compose.docker_config_check("services: {}\n", str(stack), "web1")) is None
    assert not stack.exists()                    # made for the check only, then removed
    call = docker_cli.calls()[0]
    assert call.startswith(f"compose --project-name web1 --project-directory {stack} --file {stack}/.docker-compose.check.yml")
    assert call.endswith("config --quiet")
    docker_cli.config_error("services.web.ports must be a list")
    stack.mkdir(parents=True)
    (stack / "keep").write_text("data")
    assert asyncio.run(compose.docker_config_check("x", str(stack), "web1")) == "services.web.ports must be a list"
    assert sorted(p.name for p in stack.iterdir()) == ["keep"]


# --- the real docker compose (when docker-ce-cli and docker-compose-plugin are installed) ----
def real_compose():
    try:
        return subprocess.run([compose.DOCKER, "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except OSError:
        return False


REAL = [
    ("demo-web", web_inputs(env={"TZ": "Europe/Copenhagen", "ADMIN_PASSWORD": "pa$$word with spaces: and # signs"})),
    ("demo-dns", {"name": "dns", "ports": [{"id": "dns_udp", "container": 53, "host": 53, "protocol": "udp", "ip": "192.168.1.20"},
                                           {"id": "admin", "container": 80, "host": 8081, "protocol": "tcp", "ip": ""}],
                  "volumes": [{"id": "data", "container": "/etc/dns", "source": "dnsdata", "read_only": False}],
                  "env": {"WEBPASSWORD": NASTY}, "network_mode": "proxy", "restart": "always"}),
    ("demo-host", {"name": "monitor", "ports": [], "volumes": [{"id": "data", "container": "/data", "source": "/stacks/monitor/data",
                                                                "read_only": True}],
                   "env": {}, "network_mode": "host", "restart": "on-failure"}),
]


@pytest.mark.skipif(not real_compose(), reason="needs docker-ce-cli and docker-compose-plugin")
@pytest.mark.parametrize("app, inputs", REAL, ids=[a for a, _ in REAL])
def test_real_docker_compose_accepts_the_engine_output(tmp_path, app, inputs):
    text, doc = compose.build(manifest(app), template(app), inputs, host_ip="192.168.1.20", hostname="nimbus")
    stack = tmp_path / inputs["name"]
    assert asyncio.run(compose.docker_config_check(text, str(stack), inputs["name"])) is None
    # What Compose makes of it: each value exactly as typed, nothing added. Its
    # output is a Compose file again, so "$" is shown escaped as "$$" there.
    out = subprocess.run([compose.DOCKER, "compose", "--project-directory", str(tmp_path), "-f", "-", "config", "--format", "json"],
                         input=text, capture_output=True, text=True, check=True).stdout
    service = json.loads(out)["services"][manifest(app)["main_service"]]
    for key, value in inputs["env"].items():
        assert service["environment"][key].replace("$$", "$") == value
    assert "privileged" not in service and service["restart"] == inputs["restart"]


@pytest.mark.skipif(not real_compose(), reason="needs docker-ce-cli and docker-compose-plugin")
def test_real_docker_compose_reports_problems(tmp_path):
    problem = asyncio.run(compose.docker_config_check("services:\n  web:\n    image: x\n    ports: 8080\n",
                                                      str(tmp_path / "bad"), "bad"))
    assert problem and "ports" in problem and ".docker-compose.check.yml" not in problem
