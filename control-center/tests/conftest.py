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
#         FakeLibvirt  LibvirtApi in memory: domains kept as their real XML
#                      (read back with vm_xml), volumes, states, VNC ports;
#                      fail[method] makes a call fail as libvirt would
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


# --- libvirt ---------------------------------------------------------------------------
class FakeLibvirt:
    def __init__(self, kvm=True, efi=True, threads=8, memory=16 * 1024 ** 3, nat=True,
                 images_dir="/var/lib/nimbus-vms/images"):
        self.host_info = {"kvm": kvm, "efi": efi, "threads": threads, "memory": memory}
        self.nat = nat
        self.images_dir = images_dir
        self.vms = {}            # name -> {"xml", "state", "autostart", "uuid", "cpu_time", "reason"}
        self.volumes = {}        # volume name -> capacity
        self.calls = []
        self.fail = {}           # method -> message (raised as VmError)
        self.available = True

    def _enter(self, method, *args):
        from nimbus_control.libvirt_api import UNAVAILABLE, LibvirtUnavailable, VmError
        self.calls.append((method, *args))
        if not self.available:
            raise LibvirtUnavailable(UNAVAILABLE)
        if method in self.fail:
            raise VmError(self.fail[method])

    def _vm(self, name):
        from nimbus_control.libvirt_api import VmNotFound
        if name not in self.vms:
            raise VmNotFound(f"Domain not found: no domain with matching name '{name}'")
        return self.vms[name]

    def host(self):
        self._enter("host")
        return dict(self.host_info)

    def nat_network(self):
        self._enter("nat_network")
        return {"exists": self.nat, "active": self.nat}

    def count(self):
        self._enter("count")
        return len(self.vms)

    def domains(self):
        import xml.etree.ElementTree as ET
        from nimbus_control import vm_xml
        self._enter("domains")
        out = []
        for name, vm in self.vms.items():
            info = vm_xml.parse_domain(vm["xml"])
            root = ET.fromstring(vm["xml"])
            for disk in info["disks"]:
                if disk.get("pool") == "nimbus-images" and disk.get("volume") in self.volumes:
                    disk.update(path=f"{self.images_dir}/{disk['volume']}", capacity=self.volumes[disk["volume"]],
                                allocation=0, in_pool=True)
                elif disk.get("path"):
                    disk.update(capacity=8 * 1024 ** 3, allocation=0, in_pool=False)
            unit = {"KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}[root.find("memory").get("unit", "KiB")]
            out.append({"name": name, "uuid": vm["uuid"], "state": vm["state"], "reason": vm.get("reason", ""),
                        "autostart": vm["autostart"], "persistent": True, "vcpus": int(root.findtext("vcpu")),
                        "memory": int(root.findtext("memory")) * unit, **info})
        return out

    def stats(self):
        import xml.etree.ElementTree as ET
        self._enter("stats")
        out = {}
        for vm in self.vms.values():
            if vm["state"] in ("running", "paused"):
                vcpus = int(ET.fromstring(vm["xml"]).findtext("vcpu"))
                out[vm["uuid"]] = {"cpu_time": vm["cpu_time"], "vcpus": vcpus, "rss": 512 * 1024 ** 2, "memory": 1024 ** 3}
        return out

    def vnc_target(self, name):
        from nimbus_control import vm_xml
        self._enter("vnc_target", name)
        vm = self._vm(name)
        vnc = vm_xml.parse_domain(vm["xml"])["vnc"]
        if vm["state"] not in ("running", "paused") or not vnc:
            return None
        return "127.0.0.1", 5900 + sorted(self.vms).index(name), vnc["password"]

    def define(self, xml):
        import xml.etree.ElementTree as ET
        from nimbus_control.libvirt_api import VmError
        self._enter("define", xml)
        root = ET.fromstring(xml)
        name = root.findtext("name")
        if name in self.vms:
            raise VmError(f"operation failed: domain '{name}' already exists")
        self.vms[name] = {"xml": xml, "state": "stopped", "autostart": False, "uuid": root.findtext("uuid"), "cpu_time": 0}
        return name

    def _set(self, method, name, want, new):
        from nimbus_control.libvirt_api import VmError
        self._enter(method, name)
        vm = self._vm(name)
        if want and vm["state"] not in want:
            raise VmError(f"Requested operation is not valid: domain is {vm['state']}")
        vm["state"] = new

    def start(self, name):
        self._set("start", name, ("stopped", "crashed"), "running")

    def shutdown(self, name):
        self._set("shutdown", name, ("running",), "running")       # the guest decides; nothing happens at once

    def force_off(self, name):
        self._set("force_off", name, ("running", "paused", "stopping"), "stopped")

    def reboot(self, name):
        self._set("reboot", name, ("running",), "running")

    def resume(self, name):
        self._set("resume", name, ("paused",), "running")

    def set_autostart(self, name, enabled):
        self._enter("set_autostart", name, enabled)
        self._vm(name)["autostart"] = enabled

    def undefine(self, name):
        self._enter("undefine", name)
        self._vm(name)
        del self.vms[name]

    def change_cdrom(self, name, dev, bus, path):
        import xml.etree.ElementTree as ET
        from nimbus_control import vm_xml
        self._enter("change_cdrom", name, dev, bus, path)
        vm = self._vm(name)
        root = ET.fromstring(vm["xml"])
        devices = root.find("devices")
        for i, d in enumerate(list(devices)):
            if d.tag == "disk" and d.get("device") == "cdrom" and d.find("target").get("dev") == dev:
                boot = d.find("boot")
                devices.remove(d)
                devices.insert(i, vm_xml.cdrom_xml(dev, bus, path, boot.get("order") if boot is not None else None))
        vm["xml"] = ET.tostring(root, encoding="unicode")

    def ensure_pool(self):
        self._enter("ensure_pool")

    def pool_free(self):
        self._enter("pool_free")
        return 200 * 1024 ** 3

    def create_volume(self, base, size_bytes):
        self._enter("create_volume", base, size_bytes)
        name, n = f"{base}.qcow2", 2
        while name in self.volumes:
            name, n = f"{base}-{n}.qcow2", n + 1
        self.volumes[name] = size_bytes
        return name, f"{self.images_dir}/{name}"

    def delete_volume(self, name):
        self._enter("delete_volume", name)
        del self.volumes[name]
