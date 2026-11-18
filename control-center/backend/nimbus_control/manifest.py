# =============================================================================
# nimbus_control/manifest.py: what a catalog app may declare
# =============================================================================
# What: parse_manifest(data, app_dir) checks a catalog manifest.json and
#       returns it with defaults filled in. The fields (deliverable 9's
#       manifest.schema.json and SCHEMA.md describe the same contract):
#         id, name, description, category, template_version (>= 1, NOTE-60),
#         main_service       the Compose service the engine configures
#         ports[]            id, container, host (default host port),
#                            protocol tcp|udp, bind all|lan (lan: the host's
#                            LAN address only, NOTE-18), fixed (the host port
#                            must not change, e.g. DNS on 53), description
#         volumes[]          id, container (path), kind bind|named, default
#                            (bind: a folder inside the app's stack folder;
#                            named: a Docker volume name), share_hint (bind
#                            the share of that name when it exists), owner
#                            {uid, gid} (the container's user, NOTE-31),
#                            description
#         env[]              name, default, required, secret, generate
#                            (password|hex|base64: made at install), description
#         host_paths[]       host, container, read_only: the only host paths
#                            outside the stack folder and shares this app may
#                            mount (for example the Docker socket, NOTE-30)
#         network_mode       bridge (default) | host
#         webui              {port: <port id>, path, secure_context} or null
#         notes              shown before installing
#       ManifestError says which app and which field is wrong.
# Why:  the deploy engine trusts nothing in a template it can't check here.
# =============================================================================

import re

from nimbus_common.validate import ValidationError, container_path, env_name, network_name, relative_path

APP_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SERVICE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
GENERATORS = ("password", "hex", "base64")


class ManifestError(ValueError):
    def __init__(self, app, message):
        super().__init__(f"Catalog app '{app}': {message}")
        self.app = app


def _require(cond, app, message):
    if not cond:
        raise ManifestError(app, message)


def _int(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def parse_manifest(data, app_dir):
    app = app_dir
    _require(isinstance(data, dict), app, "manifest.json must be a JSON object")
    _require(data.get("id") == app_dir and APP_ID_RE.fullmatch(app_dir or ""), app,
             f"\"id\" must equal the folder name ({app_dir})")
    for key in ("name", "description", "category"):
        _require(isinstance(data.get(key), str) and data[key].strip(), app, f"\"{key}\" must be a non-empty string")
    _require(_int(data.get("template_version"), 1, 10**6), app, "\"template_version\" must be a whole number >= 1")
    _require(isinstance(data.get("main_service"), str) and SERVICE_RE.fullmatch(data["main_service"]), app,
             "\"main_service\" must name a Compose service")
    network_mode = data.get("network_mode", "bridge")
    _require(network_mode in ("bridge", "host"), app, "\"network_mode\" is bridge or host")

    ports, seen = [], set()
    for p in data.get("ports", []):
        _require(isinstance(p, dict) and KEY_RE.fullmatch(str(p.get("id", ""))), app, "each port needs an \"id\"")
        _require(p["id"] not in seen, app, f"port id {p['id']} is used twice")
        seen.add(p["id"])
        _require(_int(p.get("container"), 1, 65535) and _int(p.get("host", p.get("container")), 1, 65535), app,
                 f"port {p['id']}: container and host ports are 1..65535")
        _require(p.get("protocol", "tcp") in ("tcp", "udp"), app, f"port {p['id']}: protocol is tcp or udp")
        _require(p.get("bind", "all") in ("all", "lan"), app, f"port {p['id']}: bind is all or lan")
        ports.append({"id": p["id"], "container": p["container"], "host": p.get("host", p["container"]),
                      "protocol": p.get("protocol", "tcp"), "bind": p.get("bind", "all"),
                      "fixed": bool(p.get("fixed", False)), "description": str(p.get("description", ""))})

    volumes, seen = [], set()
    for v in data.get("volumes", []):
        _require(isinstance(v, dict) and KEY_RE.fullmatch(str(v.get("id", ""))), app, "each volume needs an \"id\"")
        _require(v["id"] not in seen, app, f"volume id {v['id']} is used twice")
        seen.add(v["id"])
        kind = v.get("kind", "bind")
        _require(kind in ("bind", "named"), app, f"volume {v['id']}: kind is bind or named")
        try:
            container_path(v.get("container"), "container")
            if kind == "bind":
                relative_path(v.get("default", v["id"]), "default")
            else:
                network_name(v.get("default", ""), "default")
        except ValidationError as exc:
            raise ManifestError(app, f"volume {v['id']}: {exc.message}") from None
        owner = v.get("owner")
        if owner is not None:
            _require(isinstance(owner, dict) and _int(owner.get("uid"), 0, 65534) and _int(owner.get("gid", owner.get("uid")), 0, 65534),
                     app, f"volume {v['id']}: owner needs uid (and gid) 0..65534")
            owner = {"uid": owner["uid"], "gid": owner.get("gid", owner["uid"])}
        _require(v.get("share_hint") is None or kind == "bind", app, f"volume {v['id']}: share_hint needs kind bind")
        volumes.append({"id": v["id"], "container": v["container"], "kind": kind, "default": v.get("default", v["id"]),
                        "share_hint": v.get("share_hint"), "owner": owner, "description": str(v.get("description", ""))})

    env, seen = [], set()
    for e in data.get("env", []):
        try:
            env_name(e.get("name") if isinstance(e, dict) else None, "name")
        except ValidationError:
            raise ManifestError(app, "each env entry needs a valid \"name\"") from None
        _require(e["name"] not in seen, app, f"env {e['name']} is listed twice")
        seen.add(e["name"])
        _require(e.get("generate") in (None, *GENERATORS), app, f"env {e['name']}: generate is one of {GENERATORS}")
        _require(e.get("default") is None or isinstance(e["default"], str), app, f"env {e['name']}: default is a string")
        env.append({"name": e["name"], "default": e.get("default"), "required": bool(e.get("required", False)),
                    "secret": bool(e.get("secret", False)), "generate": e.get("generate"),
                    "description": str(e.get("description", ""))})

    host_paths = []
    for hp in data.get("host_paths", []):
        _require(isinstance(hp, dict) and isinstance(hp.get("host"), str) and hp["host"].startswith("/"), app,
                 "host_paths entries need an absolute \"host\" path")
        try:
            container_path(hp.get("container"), "container")
        except ValidationError as exc:
            raise ManifestError(app, f"host path {hp['host']}: {exc.message}") from None
        host_paths.append({"host": hp["host"], "container": hp["container"], "read_only": bool(hp.get("read_only", False))})

    webui = data.get("webui")
    if webui is not None:
        _require(isinstance(webui, dict) and webui.get("port") in {p["id"] for p in ports}, app,
                 "webui.port must name one of the app's ports")
        path = webui.get("path", "/")
        _require(isinstance(path, str) and path.startswith("/"), app, "webui.path starts with /")
        webui = {"port": webui["port"], "path": path, "secure_context": bool(webui.get("secure_context", False))}

    return {"id": app_dir, "name": data["name"], "description": data["description"], "category": data["category"],
            "template_version": data["template_version"], "main_service": data["main_service"],
            "network_mode": network_mode, "ports": ports, "volumes": volumes, "env": env, "host_paths": host_paths,
            "webui": webui, "notes": str(data.get("notes", ""))}
