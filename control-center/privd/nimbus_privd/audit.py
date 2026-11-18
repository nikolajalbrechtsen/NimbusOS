# =============================================================================
# nimbus_privd/audit.py: the audit log
# =============================================================================
# What: one journal entry per request and outcome, identifier nimbus-privd,
#       with structured fields: NIMBUS_VERB, NIMBUS_ARGS (secrets already
#       replaced by "***"), NIMBUS_PEER_PID, NIMBUS_OUTCOME, NIMBUS_REQUEST.
#       Read them with: journalctl -t nimbus-privd -o verbose
# Why:  spec section 3 ("writes an audit log to the journal"); docs/PLAN.md
#       §4.3: passwords are never logged.
# =============================================================================

import json
import os
import sys

ERR, WARNING, INFO = 3, 4, 6


def _stderr_is_journal():
    """True when systemd connected stderr to the journal (JOURNAL_STREAM)."""
    try:
        dev, ino = (int(x) for x in os.environ.get("JOURNAL_STREAM", "").split(":"))
        st = os.fstat(sys.stderr.fileno())
    except (ValueError, OSError):
        return False
    return st.st_dev == dev and st.st_ino == ino


def log(priority, message, **fields):
    """A structured journal entry, and the same line on stderr unless stderr
    already goes to the journal: without journald, journal.send() drops
    entries silently, and an audit log must not."""
    sent = False
    try:
        from systemd import journal
        journal.send(message, PRIORITY=str(priority), SYSLOG_IDENTIFIER="nimbus-privd",
                     **{"NIMBUS_" + k.upper(): (v if isinstance(v, str) else json.dumps(v, sort_keys=True))
                        for k, v in fields.items()})
        sent = True
    except Exception:
        sent = False
    if not (sent and _stderr_is_journal()):
        print(f"nimbus-privd: {message}", file=sys.stderr, flush=True)


def request(verb, args, peer_pid, req_id):
    log(INFO, f"{verb} requested by pid {peer_pid}", verb=verb, args=args, peer_pid=str(peer_pid),
        request=req_id)


def outcome(verb, peer_pid, req_id, outcome_code, detail=""):
    priority = INFO if outcome_code == "ok" else WARNING
    log(priority, f"{verb} by pid {peer_pid}: {outcome_code}" + (f" ({detail})" if detail else ""),
        verb=verb, peer_pid=str(peer_pid), request=req_id, outcome=outcome_code)


def denied(peer_uid, peer_pid):
    log(WARNING, f"refused a connection from uid {peer_uid} (pid {peer_pid}): only nimbus-control may call",
        peer_pid=str(peer_pid), outcome="denied")
