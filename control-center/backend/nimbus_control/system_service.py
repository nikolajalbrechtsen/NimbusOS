# =============================================================================
# nimbus_control/system_service.py: the machine itself
# =============================================================================
# What: GET  /api/system/info     host name, LAN IP (the default-route
#                                 interface), NimbusOS version and channel
#                                 (/etc/os-release)
#       GET  /api/system/summary  the sidebar footer and counts: CPU %,
#                                 memory, storage (the pool at /mnt/user when
#                                 it is mounted, else the system disk),
#                                 uptime, version, channel, pending updates
#       POST /api/system/reboot   "Restart now" (privd system.reboot)
#       Later batches add their counts (apps, VMs, shares) to the summary
#       through app.state.counters.
# Why:  spec "App shell": title bar host name and IP, sidebar footer with
#       mini CPU/RAM/pool bars, uptime, version and channel.
# =============================================================================

import os
import socket
import time

import psutil
from fastapi import APIRouter, Request

POOL_PATH = "/mnt/user"


def read_os_release(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    values[key] = value.strip().strip('"')
    except FileNotFoundError:
        pass
    return values


def default_route_iface(route_file="/proc/net/route"):
    """The interface that carries the IPv4 default route, or None."""
    try:
        with open(route_file, encoding="ascii") as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return fields[0]
    except (OSError, StopIteration):
        return None
    return None


def default_route_ip():
    """IPv4 address of the interface that carries the default route."""
    iface = default_route_iface()
    for addr in psutil.net_if_addrs().get(iface, []) if iface else []:
        if addr.family == socket.AF_INET:
            return addr.address
    return None


def version_info(os_release_path):
    rel = read_os_release(os_release_path)
    version = rel.get("VERSION", rel.get("VERSION_ID", ""))
    channel = rel.get("NIMBUSOS_CHANNEL", "")
    if channel and version.endswith(f" ({channel})"):
        version = version[: -len(f" ({channel})")]
    return {"name": rel.get("NAME", "NimbusOS"), "version": version, "channel": channel,
            "deb_version": rel.get("NIMBUSOS_DEB_VERSION", "")}


def storage_usage():
    if os.path.ismount(POOL_PATH):
        path, label = POOL_PATH, "Pool"
    else:
        path, label = "/", "System disk"
    usage = psutil.disk_usage(path)
    return {"label": label, "path": path, "used": usage.used, "total": usage.total}


router = APIRouter()


@router.get("/api/system/info")
async def info(request: Request):
    return {"hostname": socket.gethostname(), "ip": default_route_ip(),
            **version_info(request.app.state.settings.os_release)}


@router.get("/api/system/summary")
async def summary(request: Request):
    mem = psutil.virtual_memory()
    counts = {}
    for name, counter in request.app.state.counters.items():
        try:
            counts[name] = await counter()
        except Exception:
            counts[name] = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory": {"used": mem.total - mem.available, "total": mem.total},
        "storage": storage_usage(),
        "uptime_s": int(time.time() - psutil.boot_time()),
        **version_info(request.app.state.settings.os_release),
        "counts": counts,
    }


@router.post("/api/system/reboot")
async def reboot(request: Request):
    return await request.app.state.privd.call("system.reboot")
