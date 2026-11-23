#!/bin/bash
# =============================================================================
# control-center/tests/integration/docker-streams.sh
# =============================================================================
# What: reads a real Docker daemon's streaming API with the Control Center's
#       own client (nimbus_control/docker_stream.py), from the source tree,
#       in a privileged throwaway container:
#         - /events delivers "create" and "destroy" for a container as they
#           happen, through the filter metrics_service uses;
#         - /containers/<id>/stats?stream=1 sends one JSON document after
#           another in chunked framing, with the fields the formulas read
#           (cpu_stats, precpu_stats, memory_stats);
#         - an unknown container gives Docker's own error message.
#       Containers can't run here (rootless Podman gives dockerd no cgroups),
#       so the CPU and memory numbers themselves are compared with
#       "docker stats" in deliverable 11's QEMU test.
# Why:  docs/PLAN.md NOTE-69: the unit tests' FakeDockerd follows the real
#       daemon; this checks that it does.
#
# Usage: control-center/tests/integration/docker-streams.sh
#        Root, in a privileged throwaway container only
#        (tools/dev-shell.sh --privileged). Network (Docker's repository and
#        Docker Hub for busybox).
# =============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CC="$(dirname "$(dirname "$HERE")")"
REPO="$(dirname "$CC")"
[[ "$(id -u)" -eq 0 ]] || { echo "docker-streams: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] || { echo "docker-streams: throwaway container only" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends gpg ca-certificates python3 python3-psutil python3-fastapi iptables \
	>/tmp/ds-install.log 2>&1
gpg --dearmor <"$REPO/build/config/archives/docker.key.chroot" >/usr/share/keyrings/docker-test.gpg
echo "deb [signed-by=/usr/share/keyrings/docker-test.gpg] https://download.docker.com/linux/debian trixie stable" \
	>/etc/apt/sources.list.d/docker-test.list
apt-get update -q >/dev/null 2>&1
apt-get install -y -q --no-install-recommends docker-ce docker-ce-cli containerd.io >>/tmp/ds-install.log 2>&1 \
	|| { tail -n 20 /tmp/ds-install.log; exit 1; }
(dockerd --storage-driver=vfs >/tmp/dockerd.log 2>&1 &)
for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker pull -q busybox:1.37 >/dev/null
echo "Docker $(docker version --format '{{.Server.Version}}') (API $(docker version --format '{{.Server.APIVersion}}'))"

python3 - "$CC" <<'PY'
import asyncio, json, subprocess, sys
sys.path[:0] = [f"{sys.argv[1]}/backend", f"{sys.argv[1]}/common"]
from nimbus_control import docker_stream, metrics_service

passed = failed = 0
def check(name, ok):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")

async def main():
    query = {"filters": json.dumps({"type": ["container"], "event": list(metrics_service.EVENTS)})}
    seen = []
    async def events():
        async for ev in docker_stream.stream("/events", query=query):
            seen.append((ev.get("Action") or ev.get("status"), ev.get("Actor", {}).get("Attributes", {}).get("name")))
    reader = asyncio.create_task(events())
    await asyncio.sleep(0.5)
    cid = subprocess.run(["docker", "create", "--name", "probe", "--label", "com.docker.compose.project=probeapp",
                          "busybox:1.37", "sleep", "60"], capture_output=True, text=True, check=True).stdout.strip()
    await asyncio.sleep(1)
    check("/events reports the new container at once", ("create", "probe") in seen)
    docs = []
    async for doc in docker_stream.stream(f"/containers/{cid[:12]}/stats", query={"stream": "1"}):
        docs.append(doc)
        if len(docs) == 2:
            break
    check("stats?stream=1 sends document after document", len(docs) == 2)
    check("with the fields the formulas read",
          all(k in docs[0] for k in ("cpu_stats", "precpu_stats", "memory_stats")) and "cpu_usage" in docs[0]["cpu_stats"])
    check("the formulas accept a real document", isinstance(docker_stream.cpu_share(docs[1]), float)
          and isinstance(docker_stream.memory_used(docs[1]), int))
    subprocess.run(["docker", "rm", "probe"], capture_output=True, check=True)
    await asyncio.sleep(1)
    check("/events reports its removal", ("destroy", "probe") in seen)
    reader.cancel()
    try:
        async for _ in docker_stream.stream("/containers/nonexistent0/stats", query={"stream": "1"}):
            pass
        check("an unknown container is an error", False)
    except docker_stream.DockerStreamError as exc:
        check("an unknown container gives Docker's message", "404" in str(exc) and "nonexistent0" in str(exc))

asyncio.run(main())
print(f"\ndocker-streams: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PY
