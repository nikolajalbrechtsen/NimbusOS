# =============================================================================
# nimbus_control/docker_api.py: what Docker says about the apps
# =============================================================================
# What: DockerApi wraps the Docker SDK (python3-docker) for the read side:
#         containers(prefixes) every Compose-managed container with its
#                            project, service, state, exit code, health,
#                            restart count and published ports; also the
#                            unlabelled containers whose names start with
#                            one of PREFIXES (an app's companions, NOTE-29),
#                            with an empty project
#         volumes()          Docker volume names
#         published_ports()  {(protocol, host port)} in use by any container
#         networks()         Docker network names (Advanced install)
#       Calls block, so the API runs them in a worker thread. If Docker
#       isn't reachable they raise DockerUnavailable with a message to show.
#       Changes go through "docker compose" (apps_service); this is read-only.
# Why:  spec Tab A: the Docker SDK for status, logs and stats.
# =============================================================================


class DockerUnavailable(Exception):
    pass


UNAVAILABLE = "Docker isn't running, so app status can't be shown. Start it with: sudo systemctl start docker"


class DockerApi:
    def __init__(self):
        self._client = None

    def _docker(self):
        try:
            import docker
            if self._client is None:
                self._client = docker.from_env(timeout=15)
            return self._client
        except Exception as exc:
            self._client = None
            raise DockerUnavailable(UNAVAILABLE) from exc

    def containers(self, prefixes=()):
        client = self._docker()
        try:
            found = client.containers.list(all=True, filters={"label": "com.docker.compose.project"})
            ids = {c.id for c in found}
            for prefix in prefixes:                   # "name" matches anywhere in the name: checked below
                found += [c for c in client.containers.list(all=True, filters={"name": prefix})
                          if c.id not in ids and c.name.startswith(prefix)]
                ids = {c.id for c in found}
        except Exception as exc:              # the SDK's errors, and requests' when the daemon went away
            self._client = None
            raise DockerUnavailable(UNAVAILABLE) from exc
        result = []
        for c in found:
            attrs = c.attrs or {}
            state = attrs.get("State") or {}
            labels = attrs.get("Config", {}).get("Labels") or {}
            ports = []
            for key, bindings in ((attrs.get("NetworkSettings") or {}).get("Ports") or {}).items():
                container_port, _, proto = key.partition("/")
                for b in bindings or []:
                    if b.get("HostPort"):
                        ports.append({"host_ip": b.get("HostIp", ""), "host_port": int(b["HostPort"]),
                                      "container_port": int(container_port), "protocol": proto or "tcp"})
            result.append({
                "id": c.id[:12], "name": c.name,
                "project": labels.get("com.docker.compose.project", ""),
                "service": labels.get("com.docker.compose.service", ""),
                "state": state.get("Status", "unknown"), "exit_code": state.get("ExitCode", 0),
                "oom_killed": bool(state.get("OOMKilled")),
                "health": (state.get("Health") or {}).get("Status"),
                "restart_count": attrs.get("RestartCount", 0), "started_at": state.get("StartedAt", ""),
                "image": (attrs.get("Config") or {}).get("Image", ""), "ports": ports,
            })
        return result

    def published_ports(self):
        return {(p["protocol"], p["host_port"]) for c in self.containers() for p in c["ports"]}

    def networks(self):
        client = self._docker()
        try:
            return sorted(n.name for n in client.networks.list())
        except Exception as exc:
            self._client = None
            raise DockerUnavailable(UNAVAILABLE) from exc

    def volumes(self):
        client = self._docker()
        try:
            return sorted(v.name for v in client.volumes.list())
        except Exception as exc:
            self._client = None
            raise DockerUnavailable(UNAVAILABLE) from exc
