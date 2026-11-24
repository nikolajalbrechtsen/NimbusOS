# =============================================================================
# nimbus_privd/updates.py: NimbusOS update verbs
# =============================================================================
# What: thin, checked wrappers around /usr/bin/nimbus-update (nimbusos-release),
#       which holds all update logic (docs/PLAN.md §4.6):
#         update.status       nimbus-update --json status
#         update.check        nimbus-update --json check (up to 15 minutes)
#         update.set_channel  nimbus-update --json channel stable|beta
#         update.set_policy   nimbus-update --json policy automatic|notify|off
#         update.history      nimbus-update --json history --limit N
#         update.install      starts "nimbus-update install" as the transient
#                             unit nimbus-update-<job>.service (systemd-run),
#                             so it survives restarts of the services it
#                             upgrades, nimbus-control and privd included
#                             (NOTE-35); returns the job id. Refused while
#                             another one, or the timer's check or auto
#                             run, is running (not while one waits to retry).
#         update.follow       streams that unit's journal to the caller until
#                             the unit has finished, then reports its exit
#                             status. Works again after a reconnect.
# Why:  spec section 4.4 (Updates page); never a shell, fixed programs only.
# =============================================================================

import json
import secrets
import threading
import time

from nimbus_common.validate import ValidationError, int_range, job_id, one_of

from . import runner
from .registry import REGISTRY, Arg, Busy

NIMBUS_UPDATE = "/usr/bin/nimbus-update"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
UNIT_PREFIX = "nimbus-update-"
POLL_SECONDS = 1.0


def _json(argv, timeout):
    """nimbus-update --json prints one document, also when it exits non-zero
    (check exits 1 when a repository failed, and the document says which and
    how to fix it). Only output that isn't JSON is a failure."""
    proc = runner.run(argv, timeout=timeout, check=False)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise runner.CommandFailed(argv, proc.returncode, (proc.stdout or "") + (proc.stderr or "")) from None


def unit_name(job):
    return f"{UNIT_PREFIX}{job}.service"


# A unit waiting to be restarted (nimbus-update-auto.service after a busy
# exit, for 30 minutes) is "activating" to systemd but runs nothing and holds
# no lock, so it mustn't block "Install updates" (NOTE-72).
WAITING_SUBSTATES = ("auto-restart", "auto-restart-queued")


def running_update_units():
    """The nimbus-update-* units doing something now: our install jobs, and
    the timer's check and auto runs. Columns: UNIT LOAD ACTIVE SUB DESCRIPTION."""
    proc = runner.run([SYSTEMCTL, "list-units", "--type=service", "--state=activating,running",
                       "--plain", "--no-legend", "--no-pager", f"{UNIT_PREFIX}*"], timeout=15)
    units = []
    for line in proc.stdout.splitlines():
        fields = line.split()
        if fields and not (len(fields) > 3 and fields[3] in WAITING_SUBSTATES):
            units.append(fields[0])
    return units


def is_job_unit(unit):
    try:
        job_id(unit.removeprefix(UNIT_PREFIX).removesuffix(".service"))
        return True
    except ValidationError:
        return False


def unit_state(unit):
    proc = runner.run([SYSTEMCTL, "show", "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus",
                       unit], timeout=15)
    state = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        state[key] = value
    return state


def finished(state):
    active, sub = state.get("ActiveState"), state.get("SubState")
    return active in ("inactive", "failed") or (active == "active" and sub == "exited")


@REGISTRY.verb("update.status")
def status(ctx, args):
    return _json([NIMBUS_UPDATE, "--json", "status"], timeout=60)


@REGISTRY.verb("update.check", lock="updates")
def check(ctx, args):
    return _json([NIMBUS_UPDATE, "--json", "check"], timeout=900)


@REGISTRY.verb("update.set_channel", lock="updates",
               args={"channel": Arg(lambda v, f: one_of(v, ("stable", "beta"), f))})
def set_channel(ctx, args):
    return _json([NIMBUS_UPDATE, "--json", "channel", args["channel"]], timeout=300)


@REGISTRY.verb("update.set_policy", lock="updates",
               args={"policy": Arg(lambda v, f: one_of(v, ("automatic", "notify", "off"), f))})
def set_policy(ctx, args):
    return _json([NIMBUS_UPDATE, "--json", "policy", args["policy"]], timeout=60)


@REGISTRY.verb("update.history",
               args={"limit": Arg(lambda v, f: int_range(v, 1, 500, f), required=False, default=50)})
def history(ctx, args):
    return _json([NIMBUS_UPDATE, "--json", "history", "--limit", str(args["limit"])], timeout=60)


@REGISTRY.verb("update.install", lock="updates")
def install(ctx, args):
    running = running_update_units()
    if running:
        if is_job_unit(running[0]):
            raise Busy(f"Updates are already being installed ({running[0]}).")
        raise Busy(f"A scheduled update run is in progress ({running[0]}); try again when it has finished.")
    job = secrets.token_hex(6)
    runner.run([SYSTEMD_RUN, f"--unit={unit_name(job)}", "--description=NimbusOS updates (Control Center)",
                "--property=RemainAfterExit=yes", "--quiet", NIMBUS_UPDATE, "install"], timeout=30)
    return {"job": job, "unit": unit_name(job)}


@REGISTRY.verb("update.follow", stream=True, args={"job": Arg(job_id)})
def follow(ctx, args):
    unit = unit_name(args["job"])
    state = unit_state(unit)
    if state.get("LoadState") != "loaded":
        raise ValidationError("job", "There is no such update run (it may have been cleared by a restart).")

    journal = runner.start([JOURNALCTL, f"--unit={unit}", "--output=cat", "--no-pager", "--follow",
                            "--lines=all"])

    def pump():
        for line in journal.stdout:
            ctx.log(line.rstrip("\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while not finished(state):
            time.sleep(POLL_SECONDS)
            state = unit_state(unit)
        time.sleep(POLL_SECONDS)            # let the last lines through
    finally:
        journal.terminate()
        reader.join(timeout=5)
    try:
        exit_status = int(state.get("ExecMainStatus") or 0)
    except ValueError:
        exit_status = -1
    ok = exit_status == 0 and state.get("Result") in ("success", "")
    return {"job": args["job"], "exit_status": exit_status, "ok": ok, "result": state.get("Result", "")}
