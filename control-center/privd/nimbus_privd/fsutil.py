# =============================================================================
# nimbus_privd/fsutil.py: careful file changes for the storage verbs
# =============================================================================
# What: - write_atomic(path, text, mode): temporary file next to the target,
#         fsync, rename, fsync of the folder; a crash leaves the old or the new
#         file, never half of one;
#       - the managed /etc/fstab block between "# BEGIN nimbus" and
#         "# END nimbus": read_block() parses its lines into entries
#         (data and parity disks by UUID, the mergerfs pool, the ISO bind
#         mount), write_block(entries) renders them, keeps the rest of the
#         file byte for byte, saves a timestamped backup first (the last 10
#         in /var/lib/nimbus-privd/fstab-backups) and reloads systemd's mount
#         units when systemd runs;
#       - open_dir(): a folder opened with O_NOFOLLOW, for link-safe changes
#         below it.
# Why:  spec Tab C ("Edit fstab atomically and keep a backup"); docs/PLAN.md
#       §4.2 and §4.5.
# =============================================================================

import datetime
import os
import shutil

from nimbus_common import shares as S

from . import runner

FSTAB = "/etc/fstab"
BACKUPS = "/var/lib/nimbus-privd/fstab-backups"
KEEP_BACKUPS = 10
BEGIN = "# BEGIN nimbus (managed by nimbus-control; change it in the Control Center)"
END = "# END nimbus"
SYSTEMCTL = "/usr/bin/systemctl"


def write_atomic(path, text, mode=0o644):
    folder = os.path.dirname(path) or "."
    tmp = os.path.join(folder, f".{os.path.basename(path)}.nimbus-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def remove_if_marked(path, marker):
    """Delete PATH only if it is a regular file carrying MARKER (ours)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if marker not in f.read(4096):
                return False
    except (FileNotFoundError, IsADirectoryError):
        return False
    if os.path.islink(path):
        return False
    os.unlink(path)
    return True


def open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


# --- the managed fstab block ---------------------------------------------------------------
def _split(text):
    lines = text.splitlines()
    try:
        start = lines.index(BEGIN)
        end = lines.index(END, start)
    except ValueError:
        return lines, [], []
    return lines[:start], lines[start + 1:end], lines[end + 1:]


def parse_line(line):
    fields = line.split()
    if len(fields) < 4 or line.lstrip().startswith("#"):
        return {"kind": "other", "line": line}
    source, target, fstype, options = fields[:4]
    if source.startswith("UUID=") and target.startswith((S.DISKS + "/", S.PARITY + "/")):
        role = "data" if target.startswith(S.DISKS + "/") else "parity"
        return {"kind": "disk", "role": role, "label": os.path.basename(target), "uuid": source[5:], "fs": fstype}
    if target == S.POOL and fstype == "fuse.mergerfs":
        opts = dict(o.split("=", 1) if "=" in o else (o, "") for o in options.split(","))
        branches = [os.path.basename(b) for b in source.split(":")]
        return {"kind": "pool", "branches": branches, "policy": opts.get("category.create", "mfs")}
    if target == S.ISOS and "bind" in options.split(","):
        return {"kind": "isos", "source": source}
    return {"kind": "other", "line": line}


def render_entry(e):
    if e["kind"] == "disk":
        root = S.DISKS if e["role"] == "data" else S.PARITY
        passno = "2" if e["fs"] == "ext4" else "0"
        return f"UUID={e['uuid']} {root}/{e['label']} {e['fs']} defaults,nofail,x-systemd.device-timeout=30s 0 {passno}"
    if e["kind"] == "pool":
        branches = ":".join(f"{S.DISKS}/{b}" for b in e["branches"])
        requires = ",".join(f"x-systemd.requires-mounts-for={S.DISKS}/{b}" for b in e["branches"])
        return (f"{branches} {S.POOL} fuse.mergerfs defaults,nofail,allow_other,cache.files=off,dropcacheonclose=true,"
                f"category.create={e['policy']},moveonenospc=true,fsname=nimbus-pool,{requires} 0 0")
    if e["kind"] == "isos":
        return (f"{e['source']} {S.ISOS} none bind,nofail,"
                f"x-systemd.requires-mounts-for={os.path.dirname(e['source'])} 0 0")
    return e["line"]


def read_block(path=None):
    path = path or FSTAB
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    return [parse_line(line) for line in _split(text)[1] if line.strip()]


def write_block(entries, log=None, path=None):
    path = path or FSTAB
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    before, _, after = _split(text)
    order = {"disk": 0, "pool": 1, "isos": 2, "other": 3}
    body = [render_entry(e) for e in sorted(entries, key=lambda e: (order[e["kind"]], e.get("role", ""), e.get("label", "")))]
    while before and not before[-1].strip():
        before.pop()
    new = "\n".join([*before, *([""] if before else []), BEGIN, *body, END, *after]) + "\n"
    if new == text:
        return False
    if text:
        os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, os.path.join(BACKUPS, f"fstab.{stamp}"))
        for old in sorted(os.listdir(BACKUPS))[:-KEEP_BACKUPS]:
            os.unlink(os.path.join(BACKUPS, old))
    write_atomic(path, new, 0o644)
    if log:
        log(f"Updated {path} (backup in {BACKUPS})")
    if os.path.isdir("/run/systemd/system"):
        runner.run([SYSTEMCTL, "daemon-reload"], timeout=60)
    return True
