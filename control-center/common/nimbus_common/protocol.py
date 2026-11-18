# =============================================================================
# nimbus_common/protocol.py: the nimbus-privd wire protocol
# =============================================================================
# What: one request per connection on /run/nimbus-privd.sock, as JSON lines:
#         client -> privd  {"id": "...", "verb": "update.status", "args": {...}}
#         privd -> client  {"id": "...", "event": "log", "line": "..."}   (0..n)
#                          {"id": "...", "ok": true, "result": ...}       or
#                          {"id": "...", "ok": false,
#                           "error": {"code": "...", "message": "...", "field": "..."}}
#       Lines are UTF-8 JSON objects of at most MAX_LINE bytes.
# Why:  docs/PLAN.md §4.3: structured arguments only (privd renders every
#       config file itself), so there is nothing to inject into.
# =============================================================================

import json
import re

MAX_LINE = 1 << 20
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
VERB_RE = re.compile(r"[a-z]+\.[a-z_]+")

# Error codes
INVALID_REQUEST = "invalid_request"
UNKNOWN_VERB = "unknown_verb"
INVALID_ARGUMENT = "invalid_argument"
DENIED = "denied"
BUSY = "busy"
FAILED = "failed"
INTERNAL = "internal"


class ProtocolError(ValueError):
    pass


def encode(obj):
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ProtocolError("message too long")
    return data


def decode(line):
    if len(line) > MAX_LINE:
        raise ProtocolError("message too long")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"not a JSON line: {exc}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("not a JSON object")
    return obj


def parse_request(line):
    """Validate the envelope of a request line; return (id, verb, args)."""
    obj = decode(line)
    req_id, verb, args = obj.get("id"), obj.get("verb"), obj.get("args", {})
    if not isinstance(req_id, str) or not ID_RE.fullmatch(req_id):
        raise ProtocolError("missing or invalid 'id'")
    if not isinstance(verb, str) or not VERB_RE.fullmatch(verb):
        raise ProtocolError("missing or invalid 'verb'")
    if not isinstance(args, dict):
        raise ProtocolError("'args' must be an object")
    if set(obj) - {"id", "verb", "args"}:
        raise ProtocolError("unexpected fields in the request")
    return req_id, verb, args


def ok(req_id, result):
    return {"id": req_id, "ok": True, "result": result}


def error(req_id, code, message, field=None):
    err = {"code": code, "message": message}
    if field:
        err["field"] = field
    return {"id": req_id, "ok": False, "error": err}


def log_event(req_id, line):
    return {"id": req_id, "event": "log", "line": line}
