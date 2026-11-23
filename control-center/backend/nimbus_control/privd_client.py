# =============================================================================
# nimbus_control/privd_client.py: talk to nimbus-privd
# =============================================================================
# What: PrivdClient.call(verb, args) sends one request over
#       /run/nimbus-privd.sock and returns the result; .stream(verb, args)
#       also yields the log lines a long operation sends while it runs.
#       Every failure becomes PrivdError(code, message, field), with
#       "unavailable" when the socket can't be reached. The wire format is
#       nimbus_common.protocol.
# Why:  docs/PLAN.md §4.3: the backend never runs privileged commands itself.
# =============================================================================

import asyncio
import secrets

from nimbus_common import protocol


class PrivdError(Exception):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


UNAVAILABLE = ("The privileged helper isn't reachable, so this can't be done right now. "
               "Check that nimbus-privd.socket is running: systemctl status nimbus-privd.socket")


class PrivdClient:
    def __init__(self, socket_path, timeout=60.0):
        self.socket_path = socket_path
        self.timeout = timeout

    async def _open(self):
        try:
            return await asyncio.open_unix_connection(self.socket_path, limit=protocol.MAX_LINE)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError):
            raise PrivdError("unavailable", UNAVAILABLE) from None

    async def stream(self, verb, args=None, timeout=None):
        """Yield ("log", line) for each log line, then ("result", result)."""
        reader, writer = await self._open()
        req_id = secrets.token_hex(8)
        limit = timeout or self.timeout
        try:
            writer.write(protocol.encode({"id": req_id, "verb": verb, "args": args or {}}))
            await writer.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=limit)
                except TimeoutError:
                    raise PrivdError("failed", f"{verb} did not finish within {int(limit)} seconds.") from None
                except (asyncio.IncompleteReadError, ConnectionError):
                    raise PrivdError("failed", f"The privileged helper closed the connection during {verb}.") from None
                message = protocol.decode(line)
                if message.get("event") == "log":
                    yield "log", message.get("line", "")
                    continue
                if message.get("ok"):
                    yield "result", message.get("result")
                    return
                err = message.get("error") or {}
                raise PrivdError(err.get("code", "failed"), err.get("message", f"{verb} failed."), err.get("field"))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def call(self, verb, args=None, timeout=None):
        result = None
        async for kind, value in self.stream(verb, args, timeout):
            if kind == "result":
                result = value
        return result
