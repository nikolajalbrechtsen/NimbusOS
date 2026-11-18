# =============================================================================
# nimbus_privd/stacks.py: app data that containers own
# =============================================================================
# What: stack.prepare_volume {stack, path, uid, gid}
#           makes /var/lib/nimbus-control/stacks/<stack>/<path> (if missing)
#           and hands that one folder to the container's user (chown, not
#           recursive), for images that run as their own uid (Grafana: 472).
#       stack.delete_data {stack}
#           deletes /var/lib/nimbus-control/stacks/<stack> and everything in
#           it, including files the containers created; only when the user
#           ticked "delete data" when uninstalling.
#       Both walk the path one component at a time with O_NOFOLLOW: a
#       symbolic link planted inside a stack folder (the folder belongs to
#       the unprivileged nimbus-control user) can never lead them elsewhere.
#       shutil.rmtree uses the same fd-based, link-safe walk.
# Why:  docs/PLAN.md NOTE-31: the backend can't change files containers own.
# =============================================================================

import os
import shutil
import stat

from nimbus_common.validate import ValidationError, int_range, relative_path, stack_name

from .registry import REGISTRY, Arg

STACKS = "/var/lib/nimbus-control/stacks"


def _open_dir(parent_fd, name, create=False):
    """Open the directory NAME under PARENT_FD without following links."""
    if create:
        try:
            os.mkdir(name, 0o750, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise ValidationError("path", f"{name} is not a plain folder (a link or a file is in the way).") from exc


@REGISTRY.verb("stack.prepare_volume", lock="stacks", args={
    "stack": Arg(stack_name), "path": Arg(relative_path),
    "uid": Arg(lambda v, f: int_range(v, 0, 65534, f)), "gid": Arg(lambda v, f: int_range(v, 0, 65534, f))})
def prepare_volume(ctx, args):
    fds = [_open_dir(None, STACKS)]
    try:
        fds.append(_open_dir(fds[-1], args["stack"]))
        for part in args["path"].split("/"):
            fds.append(_open_dir(fds[-1], part, create=True))
        os.fchown(fds[-1], args["uid"], args["gid"])
    finally:
        for fd in reversed(fds):
            os.close(fd)
    return {"path": f"{STACKS}/{args['stack']}/{args['path']}", "uid": args["uid"], "gid": args["gid"]}


@REGISTRY.verb("stack.delete_data", lock="stacks", args={"stack": Arg(stack_name)})
def delete_data(ctx, args):
    # Relative to an O_NOFOLLOW handle on the stacks folder: if that folder
    # were swapped for a link (it belongs to nimbus-control), deleting the
    # app "etc" must not reach /etc.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("shutil.rmtree is not symlink-safe on this system")
    target = os.path.join(STACKS, args["stack"])
    root_fd = _open_dir(None, STACKS)
    try:
        try:
            st = os.stat(args["stack"], dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"deleted": None}
        if not stat.S_ISDIR(st.st_mode):
            raise ValidationError("stack", f"{target} is not a plain folder; nothing was deleted.")
        shutil.rmtree(args["stack"], dir_fd=root_fd)
    finally:
        os.close(root_fd)
    return {"deleted": target}
