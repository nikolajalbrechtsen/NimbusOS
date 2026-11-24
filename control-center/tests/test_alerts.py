# =============================================================================
# control-center/tests/test_alerts.py: the "Needs attention" rules
# =============================================================================
# What: every rule of alerts.evaluate() with the inputs that trigger it and
#       the ones that must not: apps (error, restart loop, busy), VMs (crashed,
#       paused on an error, paused by the user), disks (SMART, bad sectors,
#       temperature with the Settings limits, asleep, missing), space (pool and
#       system disk over 90 %), parity (failed, 48 hours, 10 GB), failed units,
#       updates (security, release, restart, repository, 7 days, the
#       metapackage); the worst come first; libvirt's state numbers in words.
# Why:  spec Dashboard ("Needs attention"); docs/PLAN.md §4.4, NOTE-69.
# =============================================================================

import datetime

from nimbus_control import alerts
from nimbus_control.libvirt_api import state_of

NOW = datetime.datetime(2026, 11, 23, 12, 0, tzinfo=datetime.UTC).timestamp()
HOURS = 3600


def iso(seconds_ago):
    return datetime.datetime.fromtimestamp(NOW - seconds_ago, datetime.UTC).isoformat()


def ids(found):
    return [a["id"] for a in found]


def app(name, state, reason="", busy=None):
    return {"name": name, "app_name": name.title(), "state": state, "reason": reason, "busy": busy}


def test_apps():
    found = alerts.evaluate(apps=[app("a", "error", "web exited unexpectedly (exit code 1)."), app("b", "running"),
                                  app("c", "error", "x", busy="update"), app("d", "running"), app("e", "stopped")],
                            restart_loops={"d": 4}, now=NOW)
    assert ids(found) == ["app:a", "app:d"]
    assert found[0]["title"] == "a: web exited unexpectedly (exit code 1)."
    assert found[0]["action"] == {"label": "Fix", "kind": "fix-app", "target": "a"}
    assert found[1]["title"] == "d keeps restarting (4 restarts in 5 minutes)"


def test_vms():
    vms = {"u1": {"name": "win", "state": "crashed", "reason": "its system crashed", "problem": True},
           "u2": {"name": "db", "state": "paused", "reason": "paused by a disk error (is the disk full?)", "problem": True},
           "u3": {"name": "lab", "state": "paused", "reason": "paused by the user", "problem": False},
           "u4": {"name": "ok", "state": "running", "reason": "", "problem": False}}
    found = alerts.evaluate(vms=vms, now=NOW)
    assert ids(found) == ["vm:u1", "vm:u2"]
    assert found[0]["action"] == {"label": "Start", "kind": "start-vm", "target": "win"}
    assert found[1]["title"] == "The VM db is paused by a disk error (is the disk full?)"


def disk(path, serial, **smart):
    return {"path": path, "model": "WD Red", "serial": serial, "smart": smart}


def test_disks():
    storage = {"disks": [disk("/dev/sda", "A", passed=False, reallocated=8),
                         disk("/dev/sdb", "B", passed=True, reallocated=2, pending=1, temperature=40),
                         disk("/dev/sdc", "C", passed=True, temperature=52),
                         disk("/dev/sdd", "D", passed=True, temperature=60, asleep=True),
                         disk("/dev/nvme0n1", "E", passed=True, temperature=65, nvme=True),
                         disk("/dev/nvme1n1", "F", passed=True, temperature=72, nvme=True),
                         disk("/dev/sde", "G", passed=None)],
               "fstab": [{"kind": "disk", "role": "data", "label": "disk2", "present": False},
                         {"kind": "disk", "role": "data", "label": "disk1", "present": True}]}
    found = alerts.evaluate(storage=storage, now=NOW)
    assert ids(found) == ["smart:A", "missing:disk2", "sectors:B", "temp:C", "temp:F"]
    assert found[2]["title"] == "WD Red (/dev/sdb) has 3 bad sectors"
    assert found[3]["title"] == "WD Red (/dev/sdc) is at 52 °C (limit 50 °C)"
    assert ids(alerts.evaluate(storage=storage, limits={"hdd_temp_c": 55, "nvme_temp_c": 75}, now=NOW)) == \
        ["smart:A", "missing:disk2", "sectors:B"]


def test_space():
    found = alerts.evaluate(space={"pool": {"total": 1000, "used": 950}, "system": {"total": 1000, "used": 899}}, now=NOW)
    assert ids(found) == ["full:pool"] and found[0]["title"] == "The pool is 95 % full"
    assert ids(alerts.evaluate(space={"pool": None, "system": {"total": 100, "used": 91}}, now=NOW)) == ["full:system"]


def parity(**kw):
    return {"parity": {"configured": True, "running": False, **kw}}


def test_parity():
    assert ids(alerts.evaluate(storage=parity(last={"last_sync": iso(10 * HOURS), "last_sync_ok": True},
                                              diff={"bytes": 2 * 10 ** 9}), now=NOW)) == []
    old = alerts.evaluate(storage=parity(last={"last_sync": iso(50 * HOURS), "last_sync_ok": True}), now=NOW)
    assert ids(old) == ["parity:old"] and old[0]["title"] == "Parity was last synced 50 hours ago"
    assert old[0]["action"]["kind"] == "sync-parity"
    never = alerts.evaluate(storage=parity(last={}), now=NOW)[0]
    assert never["title"] == "Parity has never been synced" and "no disk can be rebuilt" in never["detail"]
    failed = alerts.evaluate(storage=parity(last={"last_sync": iso(HOURS), "last_sync_ok": False,
                                                  "last_sync_message": "Missing disk disk2"}), now=NOW)
    assert ids(failed) == ["parity:failed"] and failed[0]["detail"] == "Missing disk disk2"
    big = alerts.evaluate(storage=parity(last={"last_sync": iso(HOURS), "last_sync_ok": True}, diff={"bytes": 12 * 10 ** 9}),
                          now=NOW)
    assert ids(big) == ["parity:unprotected"] and big[0]["title"] == "12 GB is not protected by parity yet"
    assert alerts.evaluate(storage=parity(running=True, last={}), now=NOW) == []
    assert alerts.evaluate(storage={"parity": {"configured": False}}, now=NOW) == []


def test_units_and_updates():
    found = alerts.evaluate(units=["smbd.service", "nimbus-update-0123456789ab.service"], updates={
        "counts": {"security": 2}, "update_available": True, "latest": {"version": "0.2.0"}, "reboot_required": True,
        "errors": [{"source": "nimbusos", "kind": "unreachable", "hint": "Check the internet connection."},
                   {"source": "debian", "kind": "unreachable"}],
        "checked_at": iso(8 * 86400), "desktop_metapackage_installed": False}, now=NOW)
    assert ids(found) == ["unit:smbd.service", "unit:nimbus-update-0123456789ab.service", "updates:security",
                          "updates:reboot", "updates:repo", "updates:stale", "updates:desktop", "updates:release"]
    by_id = {a["id"]: a for a in found}
    assert by_id["updates:security"]["title"] == "2 security updates are waiting"
    assert by_id["updates:release"]["severity"] == "info" and by_id["updates:release"]["title"] == "NimbusOS 0.2.0 is available"
    assert by_id["updates:repo"]["detail"] == "Check the internet connection."
    assert by_id["unit:smbd.service"]["detail"] == "Look at its log: journalctl -u smbd.service"
    calm = alerts.evaluate(updates={"counts": {"security": 0}, "checked_at": iso(HOURS), "errors": []}, now=NOW)
    assert calm == []
    assert ids(alerts.evaluate(updates={"counts": {}}, now=NOW)) == ["updates:stale"]


def test_libvirt_states_in_words():
    assert state_of(1, 1) == ("running", "")
    assert state_of(5, 3) == ("crashed", "its system crashed")
    assert state_of(6, 1) == ("crashed", "its system crashed")
    assert state_of(5, 1) == ("stopped", "")
    assert state_of(3, 5) == ("paused", "paused by a disk error (is the disk full?)")
    assert state_of(3, 1) == ("paused", "paused by the user")
    assert state_of(3, 99) == ("paused", "paused")
