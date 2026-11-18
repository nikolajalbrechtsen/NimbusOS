# =============================================================================
# nimbus_common/validate.py: input validation for the Control Center
# =============================================================================
# What: one function per kind of value. Each returns the normalized value or
#       raises ValidationError with the field name and a sentence a user can
#       act on ("Share names may use letters, digits ...").
#         stack_name, share_name, vm_name, smb_user, disk_label, port,
#         path_under (resolved with realpath; must stay inside an allowed
#         root, so ".." and symlinks can't escape), one_of, int_range,
#         job_id; for apps: app_id, relative_path (inside an app's
#         folder), env_name, network_name (also Docker volume names),
#         container_path, RESTART_POLICIES
# Why:  docs/PLAN.md §4.3 (input validation): names become directory names,
#       Samba section names, compose project names and libvirt domain names;
#       ports and paths reach docker and Samba. privd runs these again on
#       every argument it receives.
# =============================================================================

import os
import re


class ValidationError(ValueError):
    """A value was rejected. .field names the input, str() is user-facing."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


def _match(field, value, pattern, message):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValidationError(field, message)
    return value


def stack_name(value, field="name"):
    """Docker Compose project names: lowercase letters, digits, - and _."""
    return _match(field, value, r"[a-z0-9][a-z0-9_-]{0,47}",
                  "App names may use lowercase letters, digits, - and _, start with a letter or "
                  "digit, and be at most 48 characters long.")


SAMBA_RESERVED = {"global", "homes", "printers", "print$", "ipc$"}


def share_name(value, field="name"):
    """Share names become folder names and Samba section names."""
    _match(field, value, r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
           "Share names may use letters, digits, '.', '-' and '_', start with a letter or digit, "
           "and be at most 64 characters long.")
    if value.lower() in SAMBA_RESERVED:
        raise ValidationError(field, f"'{value}' is reserved by Samba; choose another name.")
    return value


def vm_name(value, field="name"):
    return _match(field, value, r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}",
                  "VM names may use letters, digits, '.', '-' and '_', start with a letter or digit, "
                  "and be at most 63 characters long.")


def smb_user(value, field="user"):
    """SMB-only users are system accounts: Debian's adduser naming rules."""
    return _match(field, value, r"[a-z_][a-z0-9_-]{0,30}",
                  "User names may use lowercase letters, digits, '-' and '_', start with a letter "
                  "or '_', and be at most 31 characters long.")


def disk_label(value, field="label"):
    """Filesystem labels; 12 characters is XFS's limit, the strictest one."""
    return _match(field, value, r"[a-z0-9][a-z0-9_-]{0,11}",
                  "Disk labels may use lowercase letters, digits, '-' and '_' and be at most "
                  "12 characters long.")


def port(value, field="port"):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValidationError(field, "Ports are whole numbers from 1 to 65535.")
    return value


def int_range(value, low, high, field):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(field, f"Must be a whole number from {low} to {high}.")
    return value


def one_of(value, choices, field):
    if value not in choices:
        raise ValidationError(field, f"Must be one of: {', '.join(choices)}.")
    return value


def job_id(value, field="id"):
    """Opaque job ids (hex), as used for transient unit names."""
    return _match(field, value, r"[0-9a-f]{12}", "Not a valid job id.")


def app_id(value, field="app"):
    """Catalog app ids (the directory names in the catalog)."""
    return _match(field, value, r"[a-z0-9][a-z0-9-]{0,62}", "Not a catalog app id.")


def relative_path(value, field="path"):
    """A path inside an app's folder: up to 8 plain components, no '.' or '..'."""
    _match(field, value, r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){0,7}",
           "Folders inside an app may use letters, digits, '.', '-' and '_', separated by '/'.")
    if any(part in (".", "..") for part in value.split("/")):
        raise ValidationError(field, "'.' and '..' are not allowed in folder names.")
    return value


def env_name(value, field="name"):
    return _match(field, value, r"[A-Za-z_][A-Za-z0-9_]{0,63}",
                  "Variable names may use letters, digits and '_', and not start with a digit.")


def network_name(value, field="network"):
    """Docker network names (also volume names)."""
    return _match(field, value, r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}",
                  "Network names may use letters, digits, '.', '-' and '_'.")


def container_path(value, field="container"):
    """An absolute path inside a container."""
    if not isinstance(value, str) or not re.fullmatch(r"/[^\x00:\n]{0,255}", value) or "/../" in f"{value}/":
        raise ValidationError(field, "Container paths are absolute, like /config.")
    return value


RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")


def path_under(value, roots, field="path", must_exist=False):
    """Resolve VALUE (absolute) with realpath; it must be one of ROOTS or
    inside one. Symbolic links are resolved first, so they can't lead out."""
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValidationError(field, "Paths must be absolute.")
    resolved = os.path.realpath(value)
    for root in roots:
        root_real = os.path.realpath(root)
        if resolved == root_real or resolved.startswith(root_real.rstrip("/") + "/"):
            if must_exist and not os.path.exists(resolved):
                raise ValidationError(field, f"{value} does not exist.")
            return resolved
    raise ValidationError(field, f"{value} is outside the allowed folders ({', '.join(roots)}).")
