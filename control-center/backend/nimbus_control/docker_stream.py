# =============================================================================
# nimbus_control/docker_stream.py: Docker's streaming API, without blocking
# =============================================================================
# What: - stream(path): an async generator over a streaming Docker API
#         endpoint on /var/run/docker.sock (HTTP/1.1 over a Unix socket),
#         yielding each JSON document as it arrives. It reads chunked and
#         plain bodies, joins documents that arrive split across chunks, and
#         closes the connection when the caller stops (task cancelled);
#       - cpu_share(stats): a container's CPU as a percentage of the whole
#         host: cpu_delta / system_delta × online_cpus gives "docker stats"
#         percent (up to 100 per core); dividing by online_cpus makes it a
#         share of the host, comparable with psutil's total;
#       - memory_used(stats): usage − inactive_file (cgroup v2) or
#         − total_inactive_file (cgroup v1), as "docker stats" shows it.
#       Used by metrics_service: one /containers/<id>/stats?stream=1 reader
#       per running container, and one /events reader for die, oom,
#       health_status and restart, so a crash shows up within a sample.
# Why:  spec Dashboard (non-blocking stats(stream=True) readers, the CPU and
#       memory formulas); docs/PLAN.md §4.4, NOTE-69.
# =============================================================================

import asyncio
import json
import urllib.parse

SOCKET = "/var/run/docker.sock"


class DockerStreamError(Exception):
    pass


async def stream(path, socket_path=SOCKET, query=None):
    """Yield the JSON documents a streaming endpoint sends."""
    if query:
        path = f"{path}?{urllib.parse.urlencode(query)}"
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=1 << 22)
    except OSError as exc:
        raise DockerStreamError(f"Docker's socket can't be opened: {exc.strerror}") from None
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode("ascii"))
        await writer.drain()
        status = await reader.readline()
        parts = status.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise DockerStreamError("Docker sent no HTTP answer.")
        code = int(parts[1])
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        if code != 200:
            body = await reader.read(4096)
            try:
                message = json.loads(body).get("message", "")
            except ValueError:
                message = body.decode("utf-8", "replace").strip()
            raise DockerStreamError(f"Docker answered {code}: {message}")
        chunked = headers.get("transfer-encoding", "").lower() == "chunked"
        buffer = b""
        while True:
            if chunked:
                size_line = await reader.readline()
                if not size_line:
                    break
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    break
                data = await reader.readexactly(size)
                await reader.readexactly(2)
            else:
                data = await reader.read(65536)
                if not data:
                    break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line)
        if buffer.strip():
            yield json.loads(buffer)
    except asyncio.IncompleteReadError:
        return
    finally:
        writer.close()


def cpu_share(stats):
    """CPU used since the previous sample, as % of the whole host (0..100)."""
    cpu, pre = stats.get("cpu_stats") or {}, stats.get("precpu_stats") or {}
    cpu_delta = ((cpu.get("cpu_usage") or {}).get("total_usage") or 0) - ((pre.get("cpu_usage") or {}).get("total_usage") or 0)
    system_delta = (cpu.get("system_cpu_usage") or 0) - (pre.get("system_cpu_usage") or 0)
    online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    if cpu_delta <= 0 or system_delta <= 0:
        return 0.0
    docker_percent = cpu_delta / system_delta * online * 100.0          # what "docker stats" shows
    return min(100.0, docker_percent / online)


def memory_used(stats):
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage") or 0
    detail = mem.get("stats") or {}
    inactive = detail.get("inactive_file", detail.get("total_inactive_file", 0)) or 0
    return usage - inactive if inactive < usage else usage
