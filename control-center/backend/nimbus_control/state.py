# =============================================================================
# nimbus_control/state.py: the Control Center's state files
# =============================================================================
# What: StateStore reads and writes the JSON state files in
#       /var/lib/nimbus-control (schema 1, NOTE-59):
#         stacks.json  {"stacks": {<name>: <record>}}: each installed app's
#                      catalog app id, template_version, install inputs and
#                      whether its Compose file was edited by hand
#         shares.json  {"shares": [...]}: read here for share_hint and the
#                      "pick a share" list; written by the shares batch
#       Writes are atomic (temporary file, fsync, rename, mode 0640).
#       Read-modify-write callers hold StateStore.lock.
# Why:  one writer, no torn files, even if the service is stopped midway.
# =============================================================================

import asyncio
import json
import os


class StateStore:
    def __init__(self, state_dir):
        self.dir = state_dir
        self.stacks_dir = os.path.join(state_dir, "stacks")
        self.lock = asyncio.Lock()

    def read(self, name, default):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        tmp = os.path.join(self.dir, f".{name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
        try:
            os.write(fd, (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    # --- stacks ----------------------------------------------------------------
    def stacks(self):
        return self.read("stacks.json", {"stacks": {}}).get("stacks", {})

    def save_stack(self, name, record):
        data = self.read("stacks.json", {"stacks": {}})
        data.setdefault("stacks", {})[name] = record
        self.write("stacks.json", data)

    def drop_stack(self, name):
        data = self.read("stacks.json", {"stacks": {}})
        data.setdefault("stacks", {}).pop(name, None)
        self.write("stacks.json", data)

    def stack_dir(self, name):
        return os.path.join(self.stacks_dir, name)

    # --- shares (written by the storage batch) --------------------------------------
    def shares(self):
        return [s for s in self.read("shares.json", {"shares": []}).get("shares", [])
                if isinstance(s, dict) and s.get("name") and s.get("path")]
