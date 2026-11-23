# =============================================================================
# nimbus_control/apps_service.py: the Apps tab (Docker App Store)
# =============================================================================
# What: - catalog: GET /api/catalog, and the icons at /catalog-icons/<id>.svg
#         (public: an <img> can't send the token; icons are not secret);
#       - planning: POST /api/apps/plan gives 1-click defaults: the first free
#         host port from each default (Docker's published ports, listening
#         sockets and the other apps' ports), a port bound to the LAN address
#         where the manifest asks (NOTE-18), each volume in the app's stack
#         folder or the share named by its share_hint, generated secrets,
#         restart unless-stopped;
#       - validation of every input (name, ports, volumes, environment,
#         network, restart) with the field named in the error, plus
#         compose.check_binds and "docker compose config" for hand-edited
#         files (POST /api/apps/validate);
#       - jobs: install, edit, start, stop, restart, update (pull + up),
#         template update, uninstall ("delete data" removes the stack folder
#         through privd, NOTE-31); each writes
#         /var/lib/nimbus-control/stacks/<name>/docker-compose.yml and runs
#         "docker compose" with an argument list;
#       - an app marked single_instance is refused a second install; an
#         app's companions (containers its own container made, NOTE-29)
#         are listed with it, and stopped and removed with it (their
#         volumes only with "delete data");
#       - status from the Docker SDK: Running, Stopped or Error (exited with
#         an error, restarting, unhealthy) per app, its ports and its web UI
#         link; "Template update available" when the catalog's
#         template_version is newer than the app's (NOTE-60), with a diff
#         (secret values hidden);
#       - live logs: WS /ws/apps/<name>/logs (docker compose logs --follow).
# Why:  spec Tab A; docs/PLAN.md NOTE-18, NOTE-29, NOTE-30, NOTE-31, NOTE-60.
# =============================================================================

import asyncio
import base64
import datetime
import difflib
import grp
import os
import re
import secrets
import socket

import psutil
from fastapi import APIRouter, Query, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nimbus_common import validate as V

from . import compose
from .auth import WS_PROTOCOL
from .docker_api import DockerUnavailable
from .errors import ApiError
from .jobs import JobFailed
from .system_service import default_route_ip

DOCKER = "/usr/bin/docker"
MAX_PORTS = 32
MASK = "••••••••"
SHARE_GROUP = "nimbus-smb"                       # the group every share's files belong to (NOTE-25)
TIMEZONE_RE = re.compile(r"[A-Za-z0-9_+-]{1,32}(/[A-Za-z0-9_+-]{1,32}){0,2}")


def host_timezone(timezone_file="/etc/timezone", localtime="/etc/localtime"):
    """This computer's time zone name for the apps' TZ (Debian keeps it in
    /etc/timezone; /etc/localtime links to the zone file); Etc/UTC if
    neither says."""
    try:
        with open(timezone_file, encoding="utf-8") as f:
            name = f.read().strip()
        if TIMEZONE_RE.fullmatch(name):
            return name
    except OSError:
        pass
    try:
        target = os.readlink(localtime)
        name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        if TIMEZONE_RE.fullmatch(name):
            return name
    except OSError:
        pass
    return "Etc/UTC"


def share_gid():
    """The gid of the shares' group, for apps that write into shares."""
    try:
        return grp.getgrnam(SHARE_GROUP).gr_gid
    except KeyError:
        return 0


def _now():
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


def generate_secret(kind):
    if kind == "hex":
        return secrets.token_hex(32)
    if kind == "base64":
        return base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return secrets.token_urlsafe(24)


class AppsService:
    def __init__(self, state, catalog, docker, privd, jobs):
        self.state = state
        self.catalog = catalog
        self.docker = docker
        self.privd = privd
        self.jobs = jobs
        # Installs still running: {name: {"app", "inputs"}}. Their names and
        # ports are taken, and the Installed list shows them as "Installing".
        self.pending = {}

    # --- helpers ----------------------------------------------------------------
    def compose_file(self, name):
        return os.path.join(self.state.stack_dir(name), "docker-compose.yml")

    def _compose(self, name, *args):
        return [DOCKER, "compose", "--ansi", "never", "--progress", "plain", "--project-name", name,
                "--file", self.compose_file(name), *args]

    def _app(self, app_id):
        entry = self.catalog.get(app_id)
        if entry is None:
            raise ApiError(404, f"The catalog has no app '{app_id}'.", "app")
        return entry

    def _record(self, name):
        record = self.state.stacks().get(name)
        if record is None:
            raise ApiError(404, f"There is no installed app named '{name}'.", "name")
        return record

    def companion_prefixes(self):
        """{container name prefix: app name} for the installed apps whose
        own container creates more containers (Nextcloud AIO, NOTE-29)."""
        out = {}
        for name, record in self.state.stacks().items():
            entry = self.catalog.get(record["app"])
            companions = entry and entry["manifest"]["companions"]
            if companions and companions["containers"]:
                out[companions["containers"]] = name
        return out

    async def _containers(self):
        try:
            return await asyncio.to_thread(self.docker.containers, tuple(self.companion_prefixes())), None
        except DockerUnavailable as exc:
            return None, str(exc)

    def _check_single(self, app_id, manifest):
        if not manifest["single_instance"]:
            return
        taken = [n for n, r in [*self.state.stacks().items(), *self.pending.items()] if r["app"] == app_id]
        if taken:
            raise ApiError(409, f"{manifest['name']} is already installed as '{taken[0]}', and it can be installed "
                                "only once (it uses fixed container and volume names).", "app")

    async def used_ports(self, exclude=None):
        """{(protocol, port): owner} for everything that holds a host port."""
        used = {}
        for name, record in [*self.state.stacks().items(), *self.pending.items()]:
            if name == exclude:
                continue
            for p in record.get("inputs", {}).get("ports", []):
                used[(p.get("protocol", "tcp"), p["host"])] = f"the app '{name}'"
        own = {(p.get("protocol", "tcp"), p["host"]) for p in
               (self.state.stacks().get(exclude, {}).get("inputs", {}).get("ports", []) if exclude else [])}
        try:
            for proto, port in await asyncio.to_thread(self.docker.published_ports):
                if (proto, port) not in own:
                    used.setdefault((proto, port), "a Docker container")
        except DockerUnavailable:
            pass
        for conn in psutil.net_connections(kind="inet"):
            if not conn.laddr:
                continue
            proto = "udp" if conn.type == socket.SOCK_DGRAM else "tcp"
            if proto == "tcp" and conn.status != psutil.CONN_LISTEN:
                continue
            if (proto, conn.laddr.port) not in own:
                used.setdefault((proto, conn.laddr.port), "another program on this computer")
        return used

    @staticmethod
    def _next_free(port, proto, used):
        while (proto, port) in used and port < 65535:
            port += 1
        return port

    # --- planning -----------------------------------------------------------------
    def unique_name(self, base):
        taken = set(self.state.stacks()) | set(self.pending)
        name, n = base[:48], 2
        while name in taken:
            name = f"{base[:44]}-{n}"
            n += 1
        return name

    async def defaults(self, app_id, name=None):
        manifest = self._app(app_id)["manifest"]
        self._check_single(app_id, manifest)
        name = V.stack_name(name) if name else self.unique_name(app_id)
        used = await self.used_ports()
        lan_ip = default_route_ip() or ""
        ports, warnings = [], []
        for p in manifest["ports"]:
            host = p["host"]
            ip = lan_ip if p["bind"] == "lan" else ""
            if p["bind"] == "lan" and not lan_ip:
                warnings.append(f"Port {p['container']}/{p['protocol']} should be bound to this computer's LAN "
                                "address, but no LAN address was found. Connect the network first.")
            if (p["protocol"], host) in used:
                if p["fixed"]:
                    warnings.append(f"Port {host}/{p['protocol']} is used by {used[(p['protocol'], host)]}, "
                                    f"and {manifest['name']} needs exactly this port.")
                else:
                    host = self._next_free(host, p["protocol"], used)
            used[(p["protocol"], host)] = "this app"
            ports.append({"id": p["id"], "container": p["container"], "host": host, "protocol": p["protocol"], "ip": ip})
        shares = {s["name"]: s["path"] for s in self.state.shares()}
        volumes = []
        for v in manifest["volumes"]:
            if v["kind"] == "named":
                source = v["default"]
            elif v["share_hint"] and v["share_hint"] in shares:
                source = shares[v["share_hint"]]
            else:
                source = os.path.join(self.state.stack_dir(name), v["default"])
            volumes.append({"id": v["id"], "container": v["container"], "source": source, "read_only": False})
        env = {}
        for e in manifest["env"]:
            env[e["name"]] = generate_secret(e["generate"]) if e["generate"] else (e["default"] or "")
        mode = manifest["network_mode"]
        return {"name": name, "ports": [] if mode == "host" else ports, "volumes": volumes, "env": env,
                "network_mode": mode, "restart": "unless-stopped"}, warnings

    async def validate_inputs(self, manifest, raw, editing=None):
        if not isinstance(raw, dict):
            raise ApiError(422, "The install settings are missing.", "inputs")
        try:
            name = V.stack_name(raw.get("name"), "name")
        except V.ValidationError as exc:
            raise ApiError(422, exc.message, exc.field) from None
        if name != editing and (name in self.state.stacks() or name in self.pending):
            raise ApiError(422, f"An app named '{name}' is already installed. Choose another name.", "name")
        stack_dir = self.state.stack_dir(name)

        mode = raw.get("network_mode", "bridge")
        if mode not in ("bridge", "host"):
            try:
                V.network_name(mode, "network_mode")
            except V.ValidationError as exc:
                raise ApiError(422, exc.message, exc.field) from None
        try:
            restart = V.one_of(raw.get("restart", "unless-stopped"), V.RESTART_POLICIES, "restart")
        except V.ValidationError as exc:
            raise ApiError(422, exc.message, exc.field) from None

        ports, seen = [], set()
        raw_ports = raw.get("ports") or []
        if mode == "host":
            raw_ports = []
        if not isinstance(raw_ports, list) or len(raw_ports) > MAX_PORTS:
            raise ApiError(422, f"At most {MAX_PORTS} port mappings.", "ports")
        fixed = {p["id"]: p for p in manifest["ports"] if p["fixed"]}
        used = await self.used_ports(exclude=editing)
        for i, p in enumerate(raw_ports):
            field = f"ports.{i}"
            try:
                container = V.port(p.get("container"), f"{field}.container")
                host = V.port(p.get("host"), f"{field}.host")
                proto = V.one_of(p.get("protocol", "tcp"), ("tcp", "udp"), f"{field}.protocol")
            except (V.ValidationError, AttributeError) as exc:
                raise ApiError(422, getattr(exc, "message", "Each port needs container and host numbers."),
                               getattr(exc, "field", field)) from None
            ip = p.get("ip") or ""
            if ip:
                try:
                    socket.inet_aton(ip)
                except OSError:
                    raise ApiError(422, f"{ip} is not an IPv4 address.", f"{field}.ip") from None
            if (proto, host) in seen:
                raise ApiError(422, f"Host port {host}/{proto} is mapped twice.", f"{field}.host")
            seen.add((proto, host))
            pid = p.get("id") if p.get("id") in {mp["id"] for mp in manifest["ports"]} else None
            if pid in fixed and host != fixed[pid]["host"]:
                raise ApiError(422, f"{manifest['name']} needs host port {fixed[pid]['host']} for this mapping.", f"{field}.host")
            if (proto, host) in used:
                suggestion = self._next_free(host, proto, {**used, **{(pr, h): "" for pr, h in seen}})
                hint = f" Use {suggestion} instead." if pid not in fixed else ""
                raise ApiError(422, f"Port {host}/{proto} is used by {used[(proto, host)]}.{hint}", f"{field}.host")
            ports.append({"id": pid, "container": container, "host": host, "protocol": proto, "ip": ip})

        volumes = []
        raw_volumes = raw.get("volumes") or []
        if not isinstance(raw_volumes, list) or len(raw_volumes) > 64:
            raise ApiError(422, "At most 64 volumes.", "volumes")
        share_paths = [s["path"] for s in self.state.shares()]
        for i, v in enumerate(raw_volumes):
            field = f"volumes.{i}"
            try:
                container = V.container_path(v.get("container"), f"{field}.container")
            except (V.ValidationError, AttributeError) as exc:
                raise ApiError(422, getattr(exc, "message", "Each volume needs a container path."), f"{field}.container") from None
            source = str(v.get("source") or "").strip()
            if not source:
                raise ApiError(422, "Choose a folder or a share for this volume.", f"{field}.source")
            if source.startswith("/"):
                try:
                    source = V.path_under(source, [stack_dir, *share_paths], f"{field}.source")
                except V.ValidationError:
                    raise ApiError(422, f"{source} is outside this app's folder ({stack_dir}) and the shares. "
                                        "Pick a share or a folder inside the app's folder.", f"{field}.source") from None
            elif "/" in source:
                try:
                    source = os.path.join(stack_dir, V.relative_path(source, f"{field}.source"))
                except V.ValidationError as exc:
                    raise ApiError(422, exc.message, exc.field) from None
            else:
                try:
                    V.network_name(source, f"{field}.source")      # a named Docker volume
                except V.ValidationError:
                    raise ApiError(422, "Use a folder path or a Docker volume name.", f"{field}.source") from None
            volumes.append({"id": v.get("id"), "container": container, "source": source, "read_only": bool(v.get("read_only"))})

        env = {}
        raw_env = raw.get("env") or {}
        if not isinstance(raw_env, dict) or len(raw_env) > 128:
            raise ApiError(422, "The environment must be a list of name and value pairs.", "env")
        for key, value in raw_env.items():
            try:
                V.env_name(key, f"env.{key}.name")
            except V.ValidationError as exc:
                raise ApiError(422, exc.message, exc.field) from None
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ApiError(422, f"The value of {key} must be text (at most 4096 characters).", f"env.{key}")
            env[key] = value
        for e in manifest["env"]:
            if e["required"] and not env.get(e["name"]):
                raise ApiError(422, f"{e['name']} is required: {e['description'] or 'it has no default'}.", f"env.{e['name']}")
        return {"name": name, "ports": ports, "volumes": volumes, "env": env, "network_mode": mode, "restart": restart}

    def _build(self, entry, inputs):
        try:
            return compose.build(entry["manifest"], entry["template"], inputs,
                                 host_ip=default_route_ip() or "", hostname=socket.gethostname(),
                                 timezone=host_timezone(), share_gid=share_gid())
        except compose.ComposeError as exc:
            raise ApiError(422, exc.message, exc.field) from None

    def _check_binds(self, name, doc, manifest):
        try:
            compose.check_binds(doc, self.state.stack_dir(name), [s["path"] for s in self.state.shares()],
                                [hp["host"] for hp in manifest["host_paths"]])
        except compose.ComposeError as exc:
            raise ApiError(422, exc.message, exc.field) from None

    # --- deploy ---------------------------------------------------------------------------
    async def _prepare_volumes(self, job, name, manifest, inputs):
        stack_dir = self.state.stack_dir(name)
        os.makedirs(stack_dir, mode=0o750, exist_ok=True)
        owners = {v["id"]: v["owner"] for v in manifest["volumes"] if v["owner"]}
        for v in inputs["volumes"]:
            src = v["source"]
            if not src.startswith(stack_dir.rstrip("/") + "/"):
                continue
            rel = os.path.relpath(src, stack_dir)
            owner = owners.get(v.get("id"))
            if owner:
                job.log(f"Preparing {src} for the container user {owner['uid']}:{owner['gid']}")
                await self.privd.call("stack.prepare_volume", {"stack": name, "path": rel,
                                                               "uid": owner["uid"], "gid": owner["gid"]})
            elif not os.path.isdir(src):
                os.makedirs(src, mode=0o750, exist_ok=True)

    def _write_compose(self, name, text):
        path = self.compose_file(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)

    def _deploy_job(self, name, entry, inputs, text, record, kind, message, new=False):
        manifest = entry["manifest"]

        async def work(job):
            try:
                problem = await compose.docker_config_check(text, self.state.stack_dir(name), name)
                if problem:
                    raise JobFailed(f"The Compose file is not valid: {problem}")
                await self._prepare_volumes(job, name, manifest, inputs)
                self._write_compose(name, text)
                # Recorded as soon as the file is written: if pulling fails
                # (no network), the app is listed with its error and can be
                # started again or uninstalled.
                async with self.state.lock:
                    self.state.save_stack(name, record)
                job.log(f"Saved to {self.compose_file(name)}")
                await job.run(self._compose(name, "pull"), cwd=self.state.stack_dir(name), timeout=3600)
                await job.run(self._compose(name, "up", "--detach", "--remove-orphans"), cwd=self.state.stack_dir(name))
            finally:
                self.pending.pop(name, None)
        job = self.jobs.start(kind, name, work, message)
        if new:
            self.pending[name] = {"app": record["app"], "inputs": inputs}
        return job

    async def install(self, app_id, raw_inputs, raw_compose=None):
        entry = self._app(app_id)
        self._check_single(app_id, entry["manifest"])
        inputs = await self.validate_inputs(entry["manifest"], raw_inputs)
        name = inputs["name"]
        text, doc = self._text_and_doc(entry, inputs, raw_compose, name)
        record = {"app": app_id, "template_version": entry["manifest"]["template_version"], "inputs": inputs, "desired": "running",
                  "custom": raw_compose is not None, "created": _now(), "updated": _now()}
        return self._deploy_job(name, entry, inputs, text, record, "install", f"{entry['manifest']['name']} is installed as '{name}'.",
                                new=True)

    def _text_and_doc(self, entry, inputs, raw_compose, name):
        if raw_compose is not None:
            if not isinstance(raw_compose, str) or len(raw_compose) > 200_000:
                raise ApiError(422, "The Compose file is missing or too long.", "compose")
            try:
                doc = compose.parse(raw_compose)
            except compose.ComposeError as exc:
                raise ApiError(422, exc.message, exc.field) from None
            text = raw_compose
        else:
            text, doc = self._build(entry, inputs)
        self._check_binds(name, doc, entry["manifest"])
        return text, doc

    async def edit(self, name, raw_inputs, raw_compose=None):
        record = self._record(name)
        entry = self._app(record["app"])
        raw_inputs = {**(raw_inputs or record["inputs"]), "name": name}
        inputs = await self.validate_inputs(entry["manifest"], raw_inputs, editing=name)
        text, _ = self._text_and_doc(entry, inputs, raw_compose, name)
        new_record = {**record, "inputs": inputs, "custom": raw_compose is not None, "desired": "running", "updated": _now()}
        return self._deploy_job(name, entry, inputs, text, new_record, "edit", f"'{name}' was updated with the new settings.")

    async def action(self, name, action):
        self._record(name)
        messages = {"start": f"'{name}' started.", "stop": f"'{name}' stopped.", "restart": f"'{name}' restarted.",
                    "update": f"'{name}' is up to date."}
        if action not in messages:
            raise ApiError(404, f"Unknown action '{action}'.")

        async def work(job):
            async with self.state.lock:
                record = self.state.stacks().get(name)
                if record is not None:
                    self.state.save_stack(name, {**record, "desired": "stopped" if action == "stop" else "running"})
            if action == "update":
                await job.run(self._compose(name, "pull"), cwd=self.state.stack_dir(name))
                await job.run(self._compose(name, "up", "--detach", "--remove-orphans"), cwd=self.state.stack_dir(name))
            elif action == "start":
                await job.run(self._compose(name, "up", "--detach"), cwd=self.state.stack_dir(name))
            else:
                await job.run(self._compose(name, action), cwd=self.state.stack_dir(name))
        return self.jobs.start(action, name, work, messages[action])

    async def _remove_companions(self, job, companions, delete_data):
        """Stop (with time to close their databases) and remove what the
        app's container created; its volumes only with DELETE_DATA."""
        prefix = companions["containers"]
        if prefix:
            found = await asyncio.to_thread(self.docker.containers, (prefix,))
            names = sorted(c["name"] for c in found if not c["project"] and c["name"].startswith(prefix))
            if names:
                await job.run([DOCKER, "stop", "--time", "60", *names])
                await job.run([DOCKER, "rm", *names])
        if companions["network"] and companions["network"] in await asyncio.to_thread(self.docker.networks):
            await job.run([DOCKER, "network", "rm", companions["network"]])
        if companions["volumes"]:
            volumes = [v for v in await asyncio.to_thread(self.docker.volumes) if v.startswith(companions["volumes"])]
            if volumes and delete_data:
                await job.run([DOCKER, "volume", "rm", *volumes])
            elif volumes:
                job.log(f"Its data stays in the Docker volumes {', '.join(volumes)}.")

    async def uninstall(self, name, delete_data):
        record = self._record(name)
        stack_dir = self.state.stack_dir(name)
        entry = self.catalog.get(record["app"])
        companions = entry and entry["manifest"]["companions"]

        async def work(job):
            if os.path.exists(self.compose_file(name)):
                await job.run(self._compose(name, "down", "--remove-orphans", *(["--volumes"] if delete_data else [])),
                              cwd=stack_dir)
            if companions:
                await self._remove_companions(job, companions, delete_data)
            if delete_data:
                job.log(f"Deleting {stack_dir}")
                await self.privd.call("stack.delete_data", {"stack": name}, timeout=600)
            else:
                job.log(f"The app's data stays in {stack_dir}.")
            async with self.state.lock:
                self.state.drop_stack(name)
        done = f"'{name}' was removed." + ("" if delete_data else f" Its data is kept in {stack_dir}.")
        return self.jobs.start("uninstall", name, work, done)

    def _template_text(self, name):
        record = self._record(name)
        entry = self._app(record["app"])
        try:
            with open(self.compose_file(name), encoding="utf-8") as f:
                current = f.read()
        except FileNotFoundError:
            current = ""
        new, doc = self._build(entry, record["inputs"])
        return record, entry, current, new, doc

    def template_diff(self, name):
        record, entry, current, new, _ = self._template_text(name)
        diff = list(difflib.unified_diff(current.splitlines(), new.splitlines(), "installed docker-compose.yml",
                                         "new template", lineterm=""))
        return {"name": name, "from": record["template_version"], "to": entry["manifest"]["template_version"],
                "custom": record.get("custom", False), "diff": self._mask_secrets(diff, entry["manifest"], record["inputs"])}

    @staticmethod
    def _mask_secrets(lines, manifest, inputs):
        """Hide secret values in a diff shown on screen: the value of each
        secret variable's own line, and the value itself wherever a template
        used it (8 characters or more, so short values can't blank out
        unrelated text)."""
        names = [e["name"] for e in manifest["env"] if e["secret"]]
        keyed = [re.compile(rf"^([ +-]\s*{re.escape(n)}:\s).+$") for n in names]
        values = sorted({form for n in names for v in [inputs["env"].get(n) or ""] if len(v) >= 8
                         for form in (v, v.replace("$", "$$"))}, key=len, reverse=True)
        out = []
        for line in lines:
            for pattern in keyed:
                line = pattern.sub(rf"\g<1>{MASK}", line)
            for value in values:
                line = line.replace(value, MASK)
            out.append(line)
        return out

    async def template_update(self, name):
        record, entry, _, new, doc = self._template_text(name)
        self._check_binds(name, doc, entry["manifest"])
        new_record = {**record, "template_version": entry["manifest"]["template_version"], "custom": False, "updated": _now()}
        return self._deploy_job(name, entry, record["inputs"], new, new_record, "template update",
                                f"'{name}' now uses template version {entry['manifest']['template_version']}.")

    # --- what's wrong (the dashboard's Fix) ----------------------------------------------------------
    async def diagnose(self, name):
        """The setting an app's error most likely comes from, as {field,
        message} for the Edit dialog to highlight: a host port another
        program holds, a folder that isn't there (a disk or share offline),
        a required variable left empty; otherwise only the reason."""
        record = self._record(name)
        containers, problem = await self._containers()
        mine = [c for c in containers or [] if c["project"] == name]
        if problem:
            return {"field": None, "message": problem}
        running = any(c["state"] == "running" for c in mine)
        if not running:
            used = await self.used_ports(exclude=name)
            own = {(p.get("protocol", "tcp"), p["host"]) for p in record["inputs"]["ports"]}
            for proto, port in own:                     # its own ports are free only while it doesn't run
                for conn in psutil.net_connections(kind="inet"):
                    if conn.laddr and conn.laddr.port == port and ((proto == "udp") == (conn.type == socket.SOCK_DGRAM)) \
                            and (proto == "udp" or conn.status == psutil.CONN_LISTEN):
                        used.setdefault((proto, port), "another program on this computer")
            for i, p in enumerate(record["inputs"]["ports"]):
                key = (p.get("protocol", "tcp"), p["host"])
                if key in used:
                    free = self._next_free(p["host"], key[0], used)
                    return {"field": f"ports.{i}.host",
                            "message": f"Port {p['host']}/{key[0]} is used by {used[key]}. Use {free} instead."}
        for i, v in enumerate(record["inputs"]["volumes"]):
            if v["source"].startswith("/") and not os.path.exists(v["source"]):
                return {"field": f"volumes.{i}.source",
                        "message": f"The folder {v['source']} isn't there. Is its disk or share offline?"}
        entry = self.catalog.get(record["app"])
        for e in (entry["manifest"]["env"] if entry else []):
            if e["required"] and not record["inputs"]["env"].get(e["name"]):
                return {"field": f"env.{e['name']}", "message": f"{e['name']} is required: {e['description']}."}
        state, reason = self.stack_status(mine, record.get("desired", "running"))
        detail = reason if state == "error" else "No setting looks wrong."
        return {"field": None, "message": f"{detail} Its log (Logs) usually says why."}

    # --- status ----------------------------------------------------------------------------------
    @staticmethod
    def stack_status(containers, desired="running"):
        """(state, reason). DESIRED is what the user last asked for: a
        container that exits while its app should run is an error; one that
        the user stopped is not, whatever its exit code."""
        if not containers:
            if desired == "stopped":
                return "stopped", "Stopped."
            return "error", "Its containers are missing. Start it to create them again."
        for c in containers:
            if c["state"] == "restarting":
                return "error", f"{c['service']} keeps restarting."
            if c["health"] == "unhealthy":
                return "error", f"{c['service']} is unhealthy."
        running = [c for c in containers if c["state"] == "running"]
        if desired == "stopped":
            return ("running", "") if running else ("stopped", "Stopped.")
        for c in containers:
            if c.get("oom_killed"):
                return "error", f"{c['service']} ran out of memory and stopped."
            if c["state"] in ("exited", "dead") and c["exit_code"] != 0:
                return "error", f"{c['service']} exited unexpectedly (exit code {c['exit_code']})."
        if not running:
            return "stopped", "Stopped."
        starting = [c for c in running if c["health"] == "starting"]
        return ("starting", f"{starting[0]['service']} is starting.") if starting else ("running", "")

    def webui_url(self, record, manifest):
        webui = manifest and manifest.get("webui")
        if not webui:
            return None
        port = next((p["host"] for p in record["inputs"]["ports"] if p.get("id") == webui["port"]), None)
        if port is None and record["inputs"]["network_mode"] == "host":
            port = next((p["host"] for p in manifest["ports"] if p["id"] == webui["port"]), None)
        if port is None:
            return None
        host = "localhost" if webui["secure_context"] else socket.gethostname()
        return f"{webui.get('scheme', 'http')}://{host}:{port}{webui['path']}"

    async def list(self, containers=None, problem=None):
        """Every app with its status. CONTAINERS (and PROBLEM, when Docker
        couldn't be read) may come from a caller that listed them already."""
        if containers is None and problem is None:
            containers, problem = await self._containers()
        by_project = {}
        for c in containers or []:
            by_project.setdefault(c["project"], []).append(c)
        records = dict(self.state.stacks())
        for name, p in self.pending.items():            # first install still running
            records.setdefault(name, {"app": p["app"], "inputs": p["inputs"], "template_version": 0, "pending": True})
        apps = []
        for name, record in sorted(records.items()):
            entry = self.catalog.get(record["app"])
            manifest = entry["manifest"] if entry else None
            if record.get("pending"):
                state, reason = "unknown", ""
                record["template_version"] = manifest["template_version"] if manifest else 0
            elif containers is None:
                state, reason = "unknown", problem
            else:
                state, reason = self.stack_status(by_project.get(name, []), record.get("desired", "running"))
            job = self.jobs.running(name)
            apps.append({
                "name": name, "app": record["app"], "app_name": manifest["name"] if manifest else record["app"],
                "icon": f"/catalog-icons/{record['app']}.svg" if entry and entry["icon"] else None,
                "state": state, "reason": reason,
                "ports": record["inputs"]["ports"], "network_mode": record["inputs"]["network_mode"],
                "webui": self.webui_url(record, manifest),
                "template_version": record["template_version"],
                "template_update": bool(manifest and manifest["template_version"] > record["template_version"]),
                "custom": record.get("custom", False), "path": self.state.stack_dir(name),
                "busy": job.kind if job else None, "job": job.id if job else None,
            })
        return {"apps": apps, "docker": {"ok": problem is None, "message": problem}}


# --- routes ------------------------------------------------------------------------------------------
router = APIRouter()


def svc(request) -> AppsService:
    return request.app.state.apps


class PlanBody(BaseModel):
    app: str
    name: str | None = None


class InstallBody(BaseModel):
    app: str
    inputs: dict | None = None
    compose: str | None = None


class EditBody(BaseModel):
    inputs: dict | None = None
    compose: str | None = None


class ValidateBody(BaseModel):
    app: str
    name: str
    compose: str


@router.get("/api/catalog")
async def catalog(request: Request):
    s = svc(request)
    installed = {}
    for name, record in s.state.stacks().items():
        installed.setdefault(record["app"], []).append(name)
    apps = [{"id": app_id, **{k: m[k] for k in ("name", "description", "category", "notes", "template_version")},
             "icon": f"/catalog-icons/{app_id}.svg" if e["icon"] else None, "installed_as": installed.get(app_id, []),
             "webui": bool(m["webui"]), "single_instance": m["single_instance"]}
            for app_id, e in sorted(s.catalog.apps().items(), key=lambda kv: kv[1]["manifest"]["name"].lower())
            for m in [e["manifest"]]]
    return {"apps": apps, "errors": s.catalog.errors}


@router.get("/catalog-icons/{app_id}.svg", include_in_schema=False)
async def icon(app_id: str, request: Request):
    try:
        entry = svc(request).catalog.get(V.app_id(app_id))
    except V.ValidationError:
        entry = None
    if not entry or not entry["icon"]:
        raise ApiError(404, "No such icon.")
    return FileResponse(entry["icon"], media_type="image/svg+xml", headers={"cache-control": "public, max-age=3600"})


@router.get("/api/apps")
async def list_apps(request: Request):
    return await svc(request).list()


@router.get("/api/apps/options")
async def options(request: Request):
    s = svc(request)
    try:
        networks = await asyncio.to_thread(s.docker.networks)
    except DockerUnavailable:
        networks = []
    return {"shares": [{"name": x["name"], "path": x["path"]} for x in s.state.shares()],
            "networks": [n for n in networks if n not in ("bridge", "host", "none")],
            "hostname": socket.gethostname(), "lan_ip": default_route_ip()}


@router.post("/api/apps/plan")
async def plan(body: PlanBody, request: Request):
    s = svc(request)
    entry = s._app(body.app)
    inputs, warnings = await s.defaults(body.app, body.name)
    text, _ = s._build(entry, inputs)
    return {"inputs": inputs, "compose": text, "warnings": warnings, "notes": entry["manifest"]["notes"],
            "path": s.state.stack_dir(inputs["name"]),
            "existing_data": os.path.isdir(s.state.stack_dir(inputs["name"])),
            "manifest": {k: entry["manifest"][k] for k in ("name", "ports", "volumes", "env", "network_mode", "webui")}}


@router.post("/api/apps/preview")
async def preview(body: InstallBody, request: Request):
    s = svc(request)
    entry = s._app(body.app)
    editing = body.inputs.get("name") if body.inputs and body.inputs.get("name") in s.state.stacks() else None
    inputs = await s.validate_inputs(entry["manifest"], body.inputs, editing=editing)
    text, _ = s._build(entry, inputs)
    return {"compose": text}


@router.post("/api/apps/validate")
async def validate_compose(body: ValidateBody, request: Request):
    s = svc(request)
    entry = s._app(body.app)
    try:
        name = V.stack_name(body.name)
    except V.ValidationError as exc:
        raise ApiError(422, exc.message, exc.field) from None
    _, doc = s._text_and_doc(entry, None, body.compose, name)
    problem = await compose.docker_config_check(body.compose, s.state.stack_dir(name), name)
    if problem:
        raise ApiError(422, f"docker compose config: {problem}", "compose")
    return {"ok": True, "services": sorted(doc["services"])}


@router.post("/api/apps")
async def install(body: InstallBody, request: Request):
    s = svc(request)
    raw = body.inputs
    if raw is None:
        raw, _ = await s.defaults(body.app)
    job = await s.install(body.app, raw, body.compose)
    return {"job": job.id, "name": job.key}


@router.get("/api/apps/{name}")
async def get_app(name: str, request: Request):
    s = svc(request)
    record = s._record(name)
    try:
        with open(s.compose_file(name), encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    entry = s.catalog.get(record["app"])
    return {"name": name, "record": record, "compose": text, "path": s.state.stack_dir(name),
            "manifest": {k: entry["manifest"][k] for k in ("name", "ports", "volumes", "env", "network_mode", "webui")}
            if entry else None}


@router.put("/api/apps/{name}")
async def edit(name: str, body: EditBody, request: Request):
    job = await svc(request).edit(name, body.inputs, body.compose)
    return {"job": job.id, "name": name}


@router.post("/api/apps/{name}/actions/{action}")
async def action(name: str, action: str, request: Request):
    job = await svc(request).action(name, action)
    return {"job": job.id, "name": name}


@router.delete("/api/apps/{name}")
async def uninstall(name: str, request: Request, delete_data: bool = Query(False)):
    job = await svc(request).uninstall(name, delete_data)
    return {"job": job.id, "name": name}


@router.get("/api/apps/{name}/diagnose")
async def diagnose(name: str, request: Request):
    return await svc(request).diagnose(name)


@router.get("/api/apps/{name}/template-diff")
async def template_diff(name: str, request: Request):
    return svc(request).template_diff(name)


@router.post("/api/apps/{name}/template-update")
async def template_update(name: str, request: Request):
    job = await svc(request).template_update(name)
    return {"job": job.id, "name": name}


@router.websocket("/ws/apps/{name}/logs")
async def logs(websocket: WebSocket, name: str):
    s = websocket.app.state.apps
    if name not in s.state.stacks():
        await websocket.close(code=4404)
        return
    await websocket.accept(subprotocol=WS_PROTOCOL)
    try:
        proc = await asyncio.create_subprocess_exec(
            *s._compose(name, "logs", "--follow", "--tail", "200", "--no-color"), cwd=s.state.stack_dir(name),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        await websocket.send_json({"type": "done", "ok": False, "message": f"docker compose could not be started: {exc.strerror}"})
        await websocket.close()
        return

    async def pump():
        async for raw in proc.stdout:
            await websocket.send_json({"type": "log", "line": raw.decode("utf-8", "replace").rstrip("\n")})
        await proc.wait()
        await websocket.send_json({"type": "done", "ok": True, "message": "The log ends here: no container of this app is running."})
        await websocket.close()

    async def watch():
        # A window that closes must stop "docker compose logs" at once, not
        # at its next log line (a quiet app may print nothing for hours).
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass

    tasks = [asyncio.create_task(pump()), asyncio.create_task(watch())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
