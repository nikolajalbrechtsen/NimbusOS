# =============================================================================
# nimbus_common/shares.py: what a share is, for the backend, privd and timers
# =============================================================================
# What: - the storage paths: data disks /mnt/disks/<label>, parity disks
#         /mnt/parity/<label>, the pool /mnt/user, the system disk's share
#         folder /srv/nimbus/shares, the VM ISO library;
#       - share_spec(): checks one share and fills in its defaults:
#           name, location (pool | system | disk:<label>), smb (exported),
#           security (public | secure | private), users ({user: rw|ro|none}),
#           browseable, recycle {enabled, days}, timemachine {enabled,
#           max_gb}, hosts_allow, notes;
#         share_list() checks a whole list (no name twice, ignoring case);
#       - samba_settings(): workgroup, description, enabled, netbios;
#       - location_root() and share_path(): the folder a share lives in,
#         computed from its name and location, never taken from input;
#       - password(): SMB passwords, 8 to 128 characters on one line.
#       privd runs these again on everything it receives (docs/PLAN.md §4.3).
# Why:  spec Tab C (Shares, Settings); one definition for every caller.
# =============================================================================

import os
import re

from .validate import ValidationError, disk_label, share_name, smb_user

DISKS = "/mnt/disks"
PARITY = "/mnt/parity"
POOL = "/mnt/user"
SYSTEM_SHARES = "/srv/nimbus/shares"
ISOS = "/var/lib/nimbus-vms/isos"
SECURITY = ("public", "secure", "private")
ACCESS = ("rw", "ro", "none")
HOST_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,62}(/\d{1,3})?|EXCEPT")


def _bool(value, field):
    if not isinstance(value, bool):
        raise ValidationError(field, "Must be on or off (true or false).")
    return value


def _int(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(field, f"Must be a whole number from {low} to {high}.")
    return value


def location(value, field="location"):
    if value in ("pool", "system"):
        return value
    if isinstance(value, str) and value.startswith("disk:"):
        disk_label(value[5:], field)
        return value
    raise ValidationError(field, "The location is the pool, the system disk, or one data disk.")


def share_spec(raw, field="share"):
    """One share, checked, with its defaults. FIELD prefixes the error's field."""
    pre = f"{field}." if field else ""
    if not isinstance(raw, dict):
        raise ValidationError(field or "share", "Each share must be an object.")
    name = share_name(raw.get("name"), f"{pre}name")
    security = raw.get("security", "private")
    if security not in SECURITY:
        raise ValidationError(f"{pre}security", "Security is Public, Secure or Private.")
    users = raw.get("users") or {}
    if not isinstance(users, dict) or len(users) > 256:
        raise ValidationError(f"{pre}users", "Users must be a map of user name to access.")
    for user, access in users.items():
        smb_user(user, f"{pre}users")
        if access not in ACCESS:
            raise ValidationError(f"{pre}users", "Access is Read/Write, Read-only or No access.")
    recycle = raw.get("recycle") or {}
    tm = raw.get("timemachine") or {}
    if not isinstance(recycle, dict) or not isinstance(tm, dict):
        raise ValidationError(f"{pre}recycle", "Recycle bin and Time Machine settings must be objects.")
    hosts = raw.get("hosts_allow") or ""
    tokens = hosts.replace(",", " ").split() if isinstance(hosts, str) else None
    if tokens is None or len(hosts) > 512 or not all(HOST_TOKEN.fullmatch(t) for t in tokens):
        raise ValidationError(f"{pre}hosts_allow", "Hosts allowed are addresses, networks (192.168.1.0/24) or "
                                                    "host names, separated by spaces.")
    notes = raw.get("notes") or ""
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ValidationError(f"{pre}notes", "Notes are at most 2000 characters.")
    return {
        "name": name, "location": location(raw.get("location", "pool"), f"{pre}location"),
        "smb": _bool(raw.get("smb", True), f"{pre}smb"), "security": security,
        "users": {u: a for u, a in sorted(users.items()) if a != "none"},
        "browseable": _bool(raw.get("browseable", True), f"{pre}browseable"),
        "recycle": {"enabled": _bool(recycle.get("enabled", False), f"{pre}recycle.enabled"),
                    "days": _int(recycle.get("days", 30), f"{pre}recycle.days", 1, 3650)},
        "timemachine": {"enabled": _bool(tm.get("enabled", False), f"{pre}timemachine.enabled"),
                        "max_gb": _int(tm.get("max_gb", 0), f"{pre}timemachine.max_gb", 0, 1_000_000)},
        "hosts_allow": " ".join(tokens), "notes": notes,
    }


def share_list(value, field="shares"):
    if not isinstance(value, list) or len(value) > 256:
        raise ValidationError(field, "A list of at most 256 shares.")
    out = [share_spec(s, f"{field}.{i}") for i, s in enumerate(value)]
    names = [s["name"].lower() for s in out]
    if len(set(names)) != len(names):
        raise ValidationError(field, "Two shares have the same name.")
    return out


def samba_settings(value, field="settings"):
    if not isinstance(value, dict):
        raise ValidationError(field, "Settings must be an object.")
    workgroup = value.get("workgroup", "WORKGROUP")
    if not isinstance(workgroup, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", workgroup):
        raise ValidationError(f"{field}.workgroup", "The workgroup is 1 to 15 letters, digits, '.', '-' or '_'.")
    description = value.get("description", "NimbusOS")
    if not isinstance(description, str) or not re.fullmatch(r"[A-Za-z0-9 ._()-]{0,64}", description):
        raise ValidationError(f"{field}.description", "The description may use letters, digits, spaces and . _ ( ) -.")
    return {"enabled": _bool(value.get("enabled", True), f"{field}.enabled"), "workgroup": workgroup,
            "description": description.strip(), "netbios": _bool(value.get("netbios", False), f"{field}.netbios")}


def location_root(loc):
    """(the folder shares at LOC live in, whether it is there now)."""
    if loc == "pool":
        return POOL, os.path.ismount(POOL)
    if loc == "system":
        return SYSTEM_SHARES, True
    root = f"{DISKS}/{loc[5:]}"
    return root, os.path.ismount(root)


def share_path(share):
    return f"{location_root(share['location'])[0]}/{share['name']}"


def password(value, field="password"):
    if not isinstance(value, str) or not 8 <= len(value) <= 128 or any(c in value for c in "\x00\n\r"):
        raise ValidationError(field, "Passwords are 8 to 128 characters, on one line.")
    return value
