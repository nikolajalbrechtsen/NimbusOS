# =============================================================================
# nimbus_control/manifest.py: what a catalog app may declare
# =============================================================================
# What: parse_manifest(data, app_dir) checks a catalog manifest.json and
#       returns it with defaults filled in. The fields (catalog/SCHEMA.md and
#       catalog/manifest.schema.json describe the same contract, and
#       tests/test_catalog.py checks that they accept the same manifests):
#         id, name, description, category, template_version (>= 1, NOTE-60),
#         main_service       the Compose service the engine configures
#         network_mode       bridge (default) | host
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
#         webui              {port: <port id>, path, scheme http|https,
#                            secure_context} or null
#         single_instance    the template fixes a container or volume name, so
#                            the app can be installed once (NOTE-29)
#         companions         {containers, volumes, network}: what the app's
#                            own container creates through the Docker socket
#                            (container and volume name prefixes, a network):
#                            counted with the app, removed with it (NOTE-29)
#         notes              shown before installing
#       Strict on purpose: unknown keys (a misspelt "share-hint"), and
#       booleans or numbers given as strings, are errors, not silently
#       ignored. ManifestError says which app and which field is wrong.
# Why:  the deploy engine trusts nothing in a template it can't check here.
# =============================================================================

import re

from nimbus_common.validate import (ValidationError, container_path, env_name, network_name, relative_path,
                                    share_name)

APP_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SERVICE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
PREFIX_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{2,62}[-_]")       # ends in - or _: a prefix, never a whole name
GENERATORS = ("password", "hex", "base64")
SCHEMES = ("http", "https")

TOP_KEYS = {"id", "name", "description", "category", "template_version", "main_service", "network_mode", "ports",
            "volumes", "env", "host_paths", "webui", "single_instance", "companions", "notes"}
PORT_KEYS = {"id", "container", "host", "protocol", "bind", "fixed", "description"}
VOLUME_KEYS = {"id", "container", "kind", "default", "share_hint", "owner", "description"}
ENV_KEYS = {"name", "default", "required", "secret", "generate", "description"}
HOST_PATH_KEYS = {"host", "container", "read_only"}
WEBUI_KEYS = {"port", "path", "scheme", "secure_context"}
COMPANION_KEYS = {"containers", "volumes", "network"}


class ManifestError(ValueError):
    def __init__(self, app, message):
        super().__init__(f"Catalog app '{app}': {message}")
        self.app = app


def _require(cond, app, message):
    if not cond:
        raise ManifestError(app, message)


def _int(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _object(value, keys, app, where):
    _require(isinstance(value, dict), app, f"{where} must be a JSON object")
    unknown = sorted(set(value) - keys)
    _require(not unknown, app, f"{where} has unknown key(s): {', '.join(unknown)}")
    return value


def _list(data, key, app):
    value = data.get(key, [])
    _require(isinstance(value, list), app, f"\"{key}\" must be a list")
    return value


def _bool(obj, key, app, where):
    value = obj.get(key, False)
    _require(isinstance(value, bool), app, f"{where}: \"{key}\" is true or false")
    return value


def _text(obj, key, app, where, default=""):
    value = obj.get(key, default)
    _require(isinstance(value, str), app, f"{where}: \"{key}\" must be a string")
    return value


def parse_manifest(data, app_dir):
    app = app_dir
    _object(data, TOP_KEYS, app, "manifest.json")
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
    for p in _list(data, "ports", app):
        _require(isinstance(p, dict) and KEY_RE.fullmatch(str(p.get("id", ""))), app, "each port needs an \"id\"")
        where = f"port {p['id']}"
        _object(p, PORT_KEYS, app, where)
        _require(p["id"] not in seen, app, f"port id {p['id']} is used twice")
        seen.add(p["id"])
        _require(_int(p.get("container"), 1, 65535) and _int(p.get("host", p.get("container")), 1, 65535), app,
                 f"{where}: container and host ports are 1..65535")
        _require(p.get("protocol", "tcp") in ("tcp", "udp"), app, f"{where}: protocol is tcp or udp")
        _require(p.get("bind", "all") in ("all", "lan"), app, f"{where}: bind is all or lan")
        ports.append({"id": p["id"], "container": p["container"], "host": p.get("host", p["container"]),
                      "protocol": p.get("protocol", "tcp"), "bind": p.get("bind", "all"),
                      "fixed": _bool(p, "fixed", app, where), "description": _text(p, "description", app, where)})

    volumes, seen = [], set()
    for v in _list(data, "volumes", app):
        _require(isinstance(v, dict) and KEY_RE.fullmatch(str(v.get("id", ""))), app, "each volume needs an \"id\"")
        where = f"volume {v['id']}"
        _object(v, VOLUME_KEYS, app, where)
        _require(v["id"] not in seen, app, f"volume id {v['id']} is used twice")
        seen.add(v["id"])
        kind = v.get("kind", "bind")
        _require(kind in ("bind", "named"), app, f"{where}: kind is bind or named")
        try:
            container_path(v.get("container"), "container")
            if kind == "bind":
                relative_path(v.get("default", v["id"]), "default")
            else:
                network_name(v.get("default", ""), "default")
            if "share_hint" in v:
                share_name(v["share_hint"], "share_hint")
        except ValidationError as exc:
            raise ManifestError(app, f"{where}: {exc.message}") from None
        owner = v.get("owner")
        if "owner" in v:
            _object(owner, {"uid", "gid"}, app, f"{where}: owner")
            _require(_int(owner.get("uid"), 0, 65534) and _int(owner.get("gid", owner.get("uid")), 0, 65534),
                     app, f"{where}: owner needs uid (and gid) 0..65534")
            owner = {"uid": owner["uid"], "gid": owner.get("gid", owner["uid"])}
        _require("share_hint" not in v or kind == "bind", app, f"{where}: share_hint needs kind bind")
        volumes.append({"id": v["id"], "container": v["container"], "kind": kind, "default": v.get("default", v["id"]),
                        "share_hint": v.get("share_hint"), "owner": owner, "description": _text(v, "description", app, where)})

    env, seen = [], set()
    for e in _list(data, "env", app):
        try:
            env_name(e.get("name") if isinstance(e, dict) else None, "name")
        except ValidationError:
            raise ManifestError(app, "each env entry needs a valid \"name\"") from None
        where = f"env {e['name']}"
        _object(e, ENV_KEYS, app, where)
        _require(e["name"] not in seen, app, f"env {e['name']} is listed twice")
        seen.add(e["name"])
        _require(e.get("generate") in (None, *GENERATORS), app, f"{where}: generate is one of {GENERATORS}")
        _require(e.get("default") is None or isinstance(e["default"], str), app, f"{where}: default is a string")
        env.append({"name": e["name"], "default": e.get("default"), "required": _bool(e, "required", app, where),
                    "secret": _bool(e, "secret", app, where), "generate": e.get("generate"),
                    "description": _text(e, "description", app, where)})

    host_paths = []
    for hp in _list(data, "host_paths", app):
        _require(isinstance(hp, dict) and isinstance(hp.get("host"), str), app, "host_paths entries need a \"host\" path")
        where = f"host path {hp['host']}"
        _object(hp, HOST_PATH_KEYS, app, where)
        try:
            container_path(hp["host"], "host")          # absolute, no "..", no ":" (it goes into "host:container")
        except ValidationError:
            raise ManifestError(app, f"{where}: needs an absolute \"host\" path, without \":\" or \"..\"") from None
        try:
            container_path(hp.get("container"), "container")
        except ValidationError as exc:
            raise ManifestError(app, f"{where}: {exc.message}") from None
        host_paths.append({"host": hp["host"], "container": hp["container"], "read_only": _bool(hp, "read_only", app, where)})

    webui = data.get("webui")
    if webui is not None:
        _object(webui, WEBUI_KEYS, app, "webui")
        _require(webui.get("port") in {p["id"] for p in ports}, app, "webui.port must name one of the app's ports")
        path = webui.get("path", "/")
        _require(isinstance(path, str) and re.fullmatch(r"/[^\s?#]*", path), app,
                 "webui.path starts with / (no spaces, query or fragment)")
        _require(webui.get("scheme", "http") in SCHEMES, app, "webui.scheme is http or https")
        webui = {"port": webui["port"], "path": path, "scheme": webui.get("scheme", "http"),
                 "secure_context": _bool(webui, "secure_context", app, "webui")}

    companions = data.get("companions")
    if "companions" in data:
        _object(companions, COMPANION_KEYS, app, "companions")
        _require(companions, app, "companions names containers, volumes and/or a network")
        for key in ("containers", "volumes"):
            _require(key not in companions or (isinstance(companions[key], str) and PREFIX_RE.fullmatch(companions[key])),
                     app, f"companions.{key} is a name prefix of at least 4 characters ending in - or _")
        try:
            if "network" in companions:
                network_name(companions["network"], "network")
        except ValidationError:
            raise ManifestError(app, "companions.network must be a Docker network name") from None
        companions = {k: companions.get(k) for k in ("containers", "volumes", "network")}

    return {"id": app_dir, "name": data["name"], "description": data["description"], "category": data["category"],
            "template_version": data["template_version"], "main_service": data["main_service"],
            "network_mode": network_mode, "ports": ports, "volumes": volumes, "env": env, "host_paths": host_paths,
            "webui": webui, "single_instance": _bool(data, "single_instance", app, "manifest.json"),
            "companions": companions, "notes": _text(data, "notes", app, "manifest.json")}
