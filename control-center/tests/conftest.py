# =============================================================================
# control-center/tests/conftest.py: shared test setup
# =============================================================================
# What: puts the backend, the shared code and privd on sys.path the way
#       /usr/lib/nimbus-control/python does on an installed system, and
#       provides the stand-ins:
#         FakePrivd    nimbus-privd in memory: records every call and answers
#                      from canned results
#         FakeDocker   the Docker SDK side (DockerApi): containers, ports and
#                      networks the test sets; container() builds one
#         docker_cli   a fixture that puts a fake "docker" program in place of
#                      /usr/bin/docker: it logs its arguments and answers
#                      compose config, pull, up and logs; fail(word) makes the
#                      subcommand WORD fail, config_error(text) makes
#                      "compose config" reject the file
#         FIXTURE_CATALOG  three demo apps (tests/fixtures/catalog)
# Why:  backend tests run without root, without a socket, without Docker and
#       without nimbus-update (control-center/tests run in CI as a normal user).
# =============================================================================

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
CC = HERE.parent
for sub in ("backend", "common", "privd"):
    sys.path.insert(0, str(CC / sub))
FIXTURE_CATALOG = HERE / "fixtures" / "catalog"

from nimbus_control.docker_api import UNAVAILABLE, DockerUnavailable  # noqa: E402
from nimbus_control.privd_client import PrivdError  # noqa: E402

STATUS = {
    "schema": 1,
    "checked_at": "2026-11-18T09:30:00Z",
    "ok": True,
    "errors": [],
    "pending": {
        "security": [{"name": "openssl", "from": "3.5.1-1", "to": "3.5.1-1+deb13u1", "origin": "Debian", "new": False}],
        "nimbusos": [{"name": "nimbus-control", "from": "0.1.0", "to": "0.1.1", "origin": "NimbusOS", "new": False}],
        "debian": [],
    },
    "counts": {"security": 1, "nimbusos": 1, "debian": 0},
    "removals": [],
    "installed": {"name": "NimbusOS", "version": "0.1.0", "pretty": "NimbusOS 0.1.0", "channel": "stable",
                  "deb_version": "0.1.0"},
    "latest": {"version": "0.1.1", "deb_version": "0.1.1", "channel": "stable", "date": "2026-11-17",
               "notes": "Fixes the share wizard on disks larger than 16 TB.\nFaster dashboard.",
               "restart_required": False},
    "update_available": True,
    "releases_error": None,
    "reboot_required": False,
    "reboot_packages": [],
    "channel": "stable",
    "policy": "automatic",
    "desktop_metapackage_installed": True,
}


class FakePrivd:
    """Answers verbs from RESULTS (value or callable(args)); raises PrivdError
    for entries that are PrivdError instances. STREAMS gives log lines."""

    def __init__(self, results=None, streams=None):
        self.calls = []
        self.results = {"update.status": STATUS, "update.check": STATUS,
                        "update.set_channel": {"channel": "beta"}, "update.set_policy": {"policy": "notify"},
                        "update.history": {"entries": []}, "update.install": {"job": "0123456789ab"},
                        "system.reboot": {"rebooting": True},
                        "stack.prepare_volume": lambda args: {"path": args["path"], "uid": args["uid"], "gid": args["gid"]},
                        "stack.delete_data": lambda args: {"deleted": args["stack"]}}
        self.results.update(results or {})
        self.streams = streams or {}

    async def stream(self, verb, args=None, timeout=None):
        self.calls.append((verb, args or {}))
        for line in self.streams.get(verb, []):
            yield "log", line
        result = self.results.get(verb)
        if isinstance(result, PrivdError):
            raise result
        yield "result", result(args) if callable(result) else result

    async def call(self, verb, args=None, timeout=None):
        result = None
        async for kind, value in self.stream(verb, args, timeout):
            if kind == "result":
                result = value
        return result


# --- Docker -------------------------------------------------------------------------
def container(project, service, state="running", exit_code=0, health=None, oom_killed=False, ports=()):
    """A container as DockerApi.containers() describes it; PORTS are (protocol, host port)."""
    return {"id": f"{project}-{service}"[:12], "name": f"{project}-{service}-1", "project": project, "service": service,
            "state": state, "exit_code": exit_code, "oom_killed": oom_killed, "health": health, "restart_count": 0,
            "started_at": "2026-11-18T09:00:00Z", "image": "docker.io/library/nginx:1.27",
            "ports": [{"host_ip": "", "host_port": p, "container_port": p, "protocol": proto} for proto, p in ports]}


class FakeDocker:
    def __init__(self, containers=None, networks=("bridge", "host", "none", "proxy")):
        self.items = list(containers or [])
        self.network_names = list(networks)
        self.available = True

    def _check(self):
        if not self.available:
            raise DockerUnavailable(UNAVAILABLE)

    def containers(self):
        self._check()
        return list(self.items)

    def published_ports(self):
        return {(p["protocol"], p["host_port"]) for c in self.containers() for p in c["ports"]}

    def networks(self):
        self._check()
        return sorted(self.network_names)


FAKE_DOCKER = """#!/bin/sh
# control-center/tests: a stand-in for /usr/bin/docker (see conftest.py).
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
args=" $* "
if [ -n "${FAKE_DOCKER_FAIL:-}" ]; then
  case "$args" in *" $FAKE_DOCKER_FAIL "*) echo "Error response from daemon: $FAKE_DOCKER_FAIL failed"; exit 1 ;; esac
fi
case "$args" in
  *" config "*) if [ -n "${FAKE_DOCKER_CONFIG_ERROR:-}" ]; then echo "$FAKE_DOCKER_CONFIG_ERROR"; exit 15; fi ;;
  *" pull "*) echo " web Pulling"; echo " web Pulled" ;;
  *" up "*) echo " Container web-1  Started" ;;
  *" logs "*) echo "web-1  | hello"; echo "web-1  | world" ;;
esac
exit 0
"""


class DockerCli:
    def __init__(self, log, monkeypatch):
        self.log = log
        self.monkeypatch = monkeypatch

    def calls(self):
        return self.log.read_text().splitlines()

    def fail(self, word):
        self.monkeypatch.setenv("FAKE_DOCKER_FAIL", word)

    def config_error(self, text):
        self.monkeypatch.setenv("FAKE_DOCKER_CONFIG_ERROR", text)


@pytest.fixture
def docker_cli(tmp_path, monkeypatch):
    from nimbus_control import apps_service, compose
    program = tmp_path / "bin" / "docker"
    program.parent.mkdir()
    program.write_text(FAKE_DOCKER)
    program.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.delenv("FAKE_DOCKER_FAIL", raising=False)
    monkeypatch.delenv("FAKE_DOCKER_CONFIG_ERROR", raising=False)
    monkeypatch.setattr(apps_service, "DOCKER", str(program))
    monkeypatch.setattr(compose, "DOCKER", str(program))
    return DockerCli(log, monkeypatch)
