"""Checks of the Control Center's systemd units that tests of the code can't see.

What: nimbus-privd.service runs in the system's own mount namespace. Any
option below makes systemd give the service its own namespace with "slave"
propagation, so mounts made inside never reach the rest of the system: the
NAS wizard's disks, mergerfs pool and ISO bind mount existed only for privd,
and Samba served folders that weren't there. The integration tests run privd
without systemd and couldn't show it; the QEMU test did. Its temporary files
go to its own runtime directory instead of a private /tmp.

Why: docs/PLAN.md NOTE-72; systemd.exec(5), MountFlags=.
"""

import configparser
import pathlib

UNITS = pathlib.Path(__file__).resolve().parents[1] / "systemd"

# Options that set up a mount namespace for the unit (systemd.exec(5)).
NAMESPACE_OPTIONS = (
    "PrivateTmp", "PrivateDevices", "PrivateMounts", "ProtectSystem", "ProtectHome",
    "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups",
    "ProtectProc", "ProcSubset", "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths",
    "ExecPaths", "NoExecPaths", "TemporaryFileSystem", "BindPaths", "BindReadOnlyPaths",
    "RootDirectory", "RootImage", "MountAPIVFS", "MountFlags",
)


def service(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNITS / name, encoding="utf-8")
    return parser["Service"]


def test_privd_mounts_reach_the_system():
    svc = service("nimbus-privd.service")
    found = [opt for opt in NAMESPACE_OPTIONS if opt in svc]
    assert found == [], f"nimbus-privd.service must not have its own mount namespace: {found}"


def test_privd_temporary_files_stay_private():
    svc = service("nimbus-privd.service")
    assert svc.get("RuntimeDirectory") == "nimbus-privd"
    assert svc.get("RuntimeDirectoryMode") == "0700"
    assert svc.get("Environment") == "TMPDIR=/run/nimbus-privd"


def test_the_web_backend_keeps_its_sandbox():
    # nimbus-control mounts nothing: it keeps its namespace, and host mounts
    # still reach it (slave propagation receives them).
    svc = service("nimbus-control.service")
    assert svc.get("ProtectSystem") == "strict"
    assert svc.get("PrivateTmp") == "yes"
