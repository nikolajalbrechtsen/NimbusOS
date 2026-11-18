# =============================================================================
# nimbus_privd/registry.py: the verb allowlist
# =============================================================================
# What: every verb privd carries out is registered here with
#         - its arguments: name -> Arg(validator, required, default), where
#           validator is a nimbus_common.validate function; an argument that
#           isn't declared is rejected;
#         - which arguments are secret (redacted in the audit log);
#         - an optional lock name: two verbs with the same lock never run at
#           the same time (the second gets "busy");
#         - whether it streams log lines while it runs.
#       A verb that isn't registered doesn't exist.
# Why:  docs/PLAN.md NOTE-15 and §4.3: a fixed allowlist, and every argument
#       re-validated by privd itself.
# =============================================================================

from dataclasses import dataclass, field
from typing import Any, Callable

from nimbus_common.validate import ValidationError


class Busy(Exception):
    """Another operation of the same kind is running."""


@dataclass(frozen=True)
class Arg:
    validator: Callable[[Any, str], Any]
    required: bool = True
    default: Any = None


@dataclass
class Verb:
    name: str
    handler: Callable
    args: dict = field(default_factory=dict)
    secret: tuple = ()
    lock: str | None = None
    stream: bool = False


class Context:
    """What a handler gets besides its arguments."""

    def __init__(self, req_id, peer_pid, emit):
        self.req_id = req_id
        self.peer_pid = peer_pid
        self._emit = emit

    def log(self, line):
        """Send one log line to the caller (streaming verbs)."""
        self._emit(str(line))


class Registry:
    def __init__(self):
        self.verbs = {}

    def verb(self, name, args=None, secret=(), lock=None, stream=False):
        def register(handler):
            if name in self.verbs:
                raise RuntimeError(f"verb {name} registered twice")
            self.verbs[name] = Verb(name, handler, dict(args or {}), tuple(secret), lock, stream)
            return handler
        return register

    def get(self, name):
        return self.verbs.get(name)

    @staticmethod
    def validate(verb, raw):
        unknown = sorted(set(raw) - set(verb.args))
        if unknown:
            raise ValidationError(unknown[0], f"Unexpected argument '{unknown[0]}'.")
        clean = {}
        for name, spec in verb.args.items():
            if name not in raw:
                if spec.required:
                    raise ValidationError(name, f"Missing argument '{name}'.")
                clean[name] = spec.default
                continue
            clean[name] = spec.validator(raw[name], name)
        return clean

    @staticmethod
    def redact(verb, args):
        return {k: ("***" if k in verb.secret else v) for k, v in args.items()}


REGISTRY = Registry()
