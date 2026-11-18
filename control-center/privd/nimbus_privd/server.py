# =============================================================================
# nimbus_privd/server.py: socket, caller check and dispatch
# =============================================================================
# What: serves the socket that systemd passes in (nimbus-privd.socket,
#       /run/nimbus-privd.sock, 0660 root:nimbus-control). For each
#       connection:
#         1. reads the caller's uid with SO_PEERCRED; anyone but the
#            nimbus-control user is refused, even group members who can
#            open the socket file;
#         2. reads one request line, checks the envelope, looks the verb up
#            in the allowlist and validates every argument;
#         3. audits the request (secrets redacted), takes the verb's lock if
#            it has one (a second caller gets "busy"), runs the handler in a
#            worker thread, streams its log lines, sends the final result.
#       Exits after IDLE_SECONDS without connections; the socket starts it
#       again on the next one.
# Why:  docs/PLAN.md §4.1, §4.3, NOTE-15.
# =============================================================================

import asyncio
import os
import pwd
import socket
import struct
import sys
import time
import traceback

from nimbus_common import protocol
from nimbus_common.validate import ValidationError

from . import audit
from .registry import REGISTRY, Busy, Context
from .runner import CommandFailed

IDLE_SECONDS = 300
SERVICE_USER = "nimbus-control"


def peer_credentials(sock):
    """(pid, uid, gid) of the process on the other end of a Unix socket."""
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)


class Server:
    def __init__(self, registry, allowed_uid, idle_seconds=IDLE_SECONDS):
        self.registry = registry
        self.allowed_uid = allowed_uid
        self.idle_seconds = idle_seconds
        self.locks = {}
        self.active = 0
        self.last_activity = time.monotonic()
        self._server = None

    # --- one connection ------------------------------------------------------
    async def handle(self, reader, writer):
        self.active += 1
        self.last_activity = time.monotonic()
        try:
            await self._handle(reader, writer)
        finally:
            self.active -= 1
            self.last_activity = time.monotonic()
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _send(self, writer, message):
        writer.write(protocol.encode(message))
        await writer.drain()

    async def _handle(self, reader, writer):
        sock = writer.get_extra_info("socket")
        pid, uid, _gid = peer_credentials(sock)
        if uid != self.allowed_uid:
            audit.denied(uid, pid)
            await self._send(writer, protocol.error("-", protocol.DENIED,
                                                    "Only the nimbus-control service may use nimbus-privd."))
            return

        try:
            line = await reader.readuntil(b"\n")
            req_id, verb_name, raw_args = protocol.parse_request(line)
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, protocol.ProtocolError) as exc:
            await self._send(writer, protocol.error("-", protocol.INVALID_REQUEST, f"Bad request: {exc}"))
            return

        verb = self.registry.get(verb_name)
        if verb is None:
            audit.outcome(verb_name, pid, req_id, protocol.UNKNOWN_VERB)
            await self._send(writer, protocol.error(req_id, protocol.UNKNOWN_VERB,
                                                    f"'{verb_name}' is not something nimbus-privd does."))
            return
        try:
            args = self.registry.validate(verb, raw_args)
        except ValidationError as exc:
            audit.outcome(verb.name, pid, req_id, protocol.INVALID_ARGUMENT, exc.message)
            await self._send(writer, protocol.error(req_id, protocol.INVALID_ARGUMENT, exc.message, exc.field))
            return

        audit.request(verb.name, self.registry.redact(verb, args), pid, req_id)
        lock = self.locks.setdefault(verb.lock, asyncio.Lock()) if verb.lock else None
        if lock is not None and lock.locked():
            audit.outcome(verb.name, pid, req_id, protocol.BUSY)
            await self._send(writer, protocol.error(req_id, protocol.BUSY,
                                                    "Another operation of this kind is running. Try again when it has finished."))
            return

        loop = asyncio.get_running_loop()

        def emit(text):
            data = protocol.encode(protocol.log_event(req_id, text))
            loop.call_soon_threadsafe(writer.write, data)

        ctx = Context(req_id, pid, emit)
        try:
            if lock is not None:
                async with lock:
                    result = await asyncio.to_thread(verb.handler, ctx, args)
            else:
                result = await asyncio.to_thread(verb.handler, ctx, args)
        except ValidationError as exc:
            reply, code, detail = protocol.error(req_id, protocol.INVALID_ARGUMENT, exc.message, exc.field), \
                protocol.INVALID_ARGUMENT, exc.message
        except Busy as exc:
            reply, code, detail = protocol.error(req_id, protocol.BUSY, str(exc)), protocol.BUSY, str(exc)
        except CommandFailed as exc:
            reply, code, detail = protocol.error(req_id, protocol.FAILED, str(exc)), protocol.FAILED, str(exc)
        except Exception as exc:
            audit.log(audit.ERR, f"{verb.name}: internal error: {exc}", verb=verb.name,
                      traceback=traceback.format_exc())
            reply, code, detail = protocol.error(req_id, protocol.INTERNAL,
                                                 f"nimbus-privd hit an internal error in {verb.name}; "
                                                 "see 'journalctl -t nimbus-privd'."), protocol.INTERNAL, str(exc)
        else:
            reply, code, detail = protocol.ok(req_id, result), "ok", ""
        audit.outcome(verb.name, pid, req_id, code, detail)
        try:
            await self._send(writer, reply)
        except (ConnectionError, OSError):
            pass

    # --- lifecycle -------------------------------------------------------------
    async def serve(self, sock):
        self._server = await asyncio.start_unix_server(self.handle, sock=sock, limit=protocol.MAX_LINE)
        async with self._server:
            while True:
                await asyncio.sleep(min(10, self.idle_seconds))
                if self.active == 0 and time.monotonic() - self.last_activity >= self.idle_seconds:
                    return


def activated_socket():
    """The listening socket from systemd socket activation (fd 3)."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or int(os.environ.get("LISTEN_FDS", "0")) < 1:
        return None
    sock = socket.socket(fileno=3)
    sock.setblocking(False)
    return sock


def main():
    sock = activated_socket()
    if sock is None:
        print("nimbus-privd: start it through nimbus-privd.socket (systemd socket activation).",
              file=sys.stderr)
        return 2
    try:
        allowed_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    except KeyError:
        print(f"nimbus-privd: the {SERVICE_USER} user does not exist.", file=sys.stderr)
        return 2
    # Importing the verb modules registers their verbs.
    from . import system, updates  # noqa: F401
    asyncio.run(Server(REGISTRY, allowed_uid).serve(sock))
    return 0
