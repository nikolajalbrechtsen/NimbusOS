# =============================================================================
# nimbus_control/compose.py: from catalog template to docker-compose.yml
# =============================================================================
# What: build(manifest, template, inputs, ...) renders an app's
#       docker-compose.yml.j2 and returns the final Compose text:
#         1. Jinja2 SandboxedEnvironment with StrictUndefined. Templates see
#            stack, env (resolved values), ports (id -> host port), host_ip,
#            hostname, timezone (this computer's, e.g. Europe/Copenhagen) and
#            share_gid (the nimbus-smb group, for apps that write into
#            shares); every value goes through the "q" filter, which writes
#            it as a JSON string (valid YAML) with "$" doubled so Compose
#            doesn't interpolate it. Nothing a user types can add YAML.
#         2. The engine then sets the main service's ports, volumes,
#            environment, network mode and restart policy from the install
#            inputs, and the project name, so 1-click and Advanced install
#            produce the same shape. In bridge mode the service joins the
#            project's network, unless the template asks for Docker's
#            default bridge itself ("network_mode: bridge", as Nextcloud
#            AIO's own Compose file does).
#       check_binds(doc, ...) enforces NOTE-30 on every deploy, hand-edited
#       or not: a bind mount must resolve inside the app's stack folder,
#       inside a share, or be one of the app's declared host paths.
#       docker_config_check() runs "docker compose config" on the result.
# Why:  spec Tab A (deploy engine, Advanced install) and docs/PLAN.md NOTE-30.
# =============================================================================

import asyncio
import json
import os

import yaml
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

DOCKER = "/usr/bin/docker"


class ComposeError(ValueError):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


def q(value):
    """Quote a value for a Compose file: a JSON string, with "$" escaped."""
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value).replace("$", "$$"))


def _environment():
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    env.filters["q"] = q
    return env


def render(template, context):
    try:
        return _environment().from_string(template).render(**context)
    except TemplateError as exc:
        raise ComposeError(f"The app's template could not be filled in: {exc}") from None


def _escape(value):
    return str(value).replace("$", "$$")


def port_spec(port):
    ip = f"{port['ip']}:" if port.get("ip") else ""
    return f"{ip}{port['host']}:{port['container']}/{port.get('protocol', 'tcp')}"


def volume_spec(volume):
    return f"{volume['source']}:{volume['container']}" + (":ro" if volume.get("read_only") else "")


def build(manifest, template, inputs, *, host_ip="", hostname="", timezone="Etc/UTC", share_gid=0):
    """Return (compose_text, compose_dict) for INPUTS (see apps_service)."""
    context = {
        "stack": inputs["name"],
        "env": dict(inputs["env"]),
        "ports": {p["id"]: p["host"] for p in inputs["ports"] if p.get("id")},
        "host_ip": host_ip,
        "hostname": hostname,
        "timezone": timezone,
        "share_gid": share_gid,
    }
    text = render(template, context)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ComposeError(f"The app's template produced invalid YAML: {exc}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) \
            or manifest["main_service"] not in doc["services"]:
        raise ComposeError(f"The app's template has no service '{manifest['main_service']}'.")

    doc["name"] = inputs["name"]
    svc = doc["services"][manifest["main_service"]] or {}
    doc["services"][manifest["main_service"]] = svc
    svc["restart"] = inputs["restart"]
    mode = inputs["network_mode"]
    if mode == "host":
        svc["network_mode"] = "host"
        svc.pop("ports", None)
        svc.pop("networks", None)
        svc.pop("hostname", None)                     # Docker refuses one with host networking: it's the computer's
    else:
        if not (mode == "bridge" and svc.get("network_mode") == "bridge"):
            svc.pop("network_mode", None)
        svc["ports"] = [port_spec(p) for p in inputs["ports"]]
        if not svc["ports"]:
            svc.pop("ports")
        if mode != "bridge":
            svc["networks"] = [mode]
            doc.setdefault("networks", {})[mode] = {"external": True}

    svc["volumes"] = [volume_spec(v) for v in inputs["volumes"]] + \
        [volume_spec({"source": hp["host"], "container": hp["container"], "read_only": hp["read_only"]})
         for hp in manifest["host_paths"]]
    if not svc["volumes"]:
        svc.pop("volumes")
    # Named volumes stay in the project's scope (Docker calls them
    # <stack>_<volume>), so two installs of one app never share a database.
    # A template that needs one fixed name (Nextcloud AIO, NOTE-29) declares
    # the volume with "name:" itself; that declaration is kept.
    named = [v["source"] for v in inputs["volumes"] if not v["source"].startswith("/")]
    for name in named:
        volumes = doc.get("volumes") if isinstance(doc.get("volumes"), dict) else {}
        doc["volumes"] = volumes
        if not isinstance(volumes.get(name), dict):
            volumes[name] = {}

    environment = svc.get("environment") or {}
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) if "=" in item else (item, "") for item in environment)
    # Sorted: the same inputs give the same file byte for byte (the state
    # file stores them sorted), so a template diff shows only real changes.
    environment.update({k: _escape(v) for k, v in sorted(inputs["env"].items())})
    if environment:
        svc["environment"] = environment
    else:
        svc.pop("environment", None)

    out = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return out, doc


def parse(text):
    """Parse a hand-edited Compose file."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ComposeError(f"This is not valid YAML: {exc}", "compose") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise ComposeError("A Compose file needs a \"services:\" section with at least one service.", "compose")
    return doc


def _bind_sources(service):
    for entry in service.get("volumes") or []:
        if isinstance(entry, str):
            source = entry.split(":", 1)[0] if ":" in entry else None
            if source and (source.startswith(("/", ".", "~"))):
                yield source
        elif isinstance(entry, dict) and entry.get("type", "volume") == "bind":
            yield str(entry.get("source", ""))


def check_binds(doc, stack_dir, share_paths, host_paths):
    """Raise ComposeError naming the first bind mount outside the allowed
    places: the stack folder, a share, or an exact declared host path."""
    allowed_roots = [os.path.realpath(stack_dir)] + [os.path.realpath(p) for p in share_paths]
    exact = {os.path.realpath(p) for p in host_paths} | set(host_paths)
    for name, service in (doc.get("services") or {}).items():
        if not isinstance(service, dict):
            raise ComposeError(f"Service '{name}' is not a mapping.", "compose")
        for source in _bind_sources(service):
            if source.startswith("~"):
                raise ComposeError(f"Service '{name}': home folders (~) can't be mounted. Use a share instead.", "compose")
            path = source if source.startswith("/") else os.path.join(stack_dir, source)
            real = os.path.realpath(path)
            if source in exact or real in exact:
                continue
            if any(real == root or real.startswith(root.rstrip("/") + "/") for root in allowed_roots):
                continue
            raise ComposeError(
                f"Service '{name}': the folder {source} is outside this app's folder ({stack_dir}) and the shares. "
                "Mount a folder inside the app's folder or a share instead.", "compose")


async def docker_config_check(text, stack_dir, project):
    """Run "docker compose config" on TEXT; return None or the error. The
    stack folder is made for the check if needed and removed again if the
    check was all that used it (an app that was never installed leaves no
    folder behind)."""
    created = not os.path.isdir(stack_dir)
    os.makedirs(stack_dir, mode=0o750, exist_ok=True)
    path = os.path.join(stack_dir, ".docker-compose.check.yml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        proc = await asyncio.create_subprocess_exec(
            DOCKER, "compose", "--project-name", project, "--project-directory", stack_dir, "--file", path,
            "config", "--quiet", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except FileNotFoundError:
        return "The docker command is missing (docker-ce-cli), so the Compose file can't be checked."
    except asyncio.TimeoutError:
        return "Checking the Compose file took too long."
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        if created:
            try:
                os.rmdir(stack_dir)
            except OSError:
                pass
    if proc.returncode != 0:
        return out.decode("utf-8", "replace").strip().replace(path, "docker-compose.yml") or "docker compose config failed."
    return None
