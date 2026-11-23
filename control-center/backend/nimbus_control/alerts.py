# =============================================================================
# nimbus_control/alerts.py: what "Needs attention" shows
# =============================================================================
# What: evaluate(...) turns the collector's latest view into a list of
#       alerts, worst first. Each has a stable id, a severity (bad, warn,
#       info), a one-line title, an optional detail, and an action (what the
#       dashboard's button does). The rules:
#         apps      in an error state (exited unexpectedly while it should
#                   run, out of memory, unhealthy, restarting, containers
#                   missing), or restarted 3 times or more in 5 minutes;
#                   not while a job is working on the app
#         VMs       crashed, or paused by a disk error, its watchdog or a
#                   guest crash
#         disks     SMART failing; reallocated, pending or media errors; a
#                   temperature over the limit in Settings (hard disks and
#                   SSDs 50 °C, NVMe 70 °C by default; not from a reading
#                   taken before the disk fell asleep); a set-up disk that is
#                   missing
#         storage   the pool, or the system disk, more than 90 % full
#         parity    the last sync failed; no sync for more than 48 hours; more
#                   than 10 GB not yet protected
#         services  a failed nimbus-*, smbd, wsdd2, docker or libvirtd unit
#         updates   security updates waiting; a new NimbusOS release; a restart
#                   needed; the NimbusOS repository failing (with nimbus-update's
#                   hint); not checked for more than 7 days; the
#                   nimbusos-desktop metapackage missing (NOTE-48)
# Why:  spec Dashboard ("Needs attention"); docs/PLAN.md §4.4, NOTE-69.
# =============================================================================

import datetime

RESTART_LIMIT = 3
RESTART_WINDOW = 300
FULL = 0.90
PARITY_MAX_AGE = 48 * 3600
UNPROTECTED_MAX = 10 * 1000 ** 3
CHECK_MAX_AGE = 7 * 86400
ORDER = {"bad": 0, "warn": 1, "info": 2}


def _age(iso, now):
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.UTC)
    return now - t.timestamp()


def _alert(aid, severity, title, detail=None, label=None, kind=None, target=None):
    action = {"label": label, "kind": kind, "target": target} if label else None
    return {"id": aid, "severity": severity, "title": title, "detail": detail, "action": action}


def _size(n):
    return f"{n / 1000 ** 3:.0f} GB" if n >= 1000 ** 3 else f"{n / 1000 ** 2:.0f} MB"


def app_alerts(apps, restart_loops):
    out = []
    for app in apps:
        if app.get("busy"):
            continue
        loops = restart_loops.get(app["name"])
        if app["state"] == "error":
            out.append(_alert(f"app:{app['name']}", "bad", f"{app['name']}: {app['reason'] or 'not working'}",
                              f"{app['app_name']} should be running.", "Fix", "fix-app", app["name"]))
        elif loops:
            out.append(_alert(f"app:{app['name']}", "bad", f"{app['name']} keeps restarting ({loops} restarts in 5 minutes)",
                              f"{app['app_name']} starts and stops again.", "Fix", "fix-app", app["name"]))
    return out


def vm_alerts(vms):
    out = []
    for uid, vm in vms.items():
        if not vm.get("problem"):
            continue
        if vm["state"] == "crashed":
            out.append(_alert(f"vm:{uid}", "bad", f"The VM {vm['name']} crashed", "Its system stopped by itself.",
                              "Start", "start-vm", vm["name"]))
        else:
            out.append(_alert(f"vm:{uid}", "bad", f"The VM {vm['name']} is {vm['reason']}", None,
                              "Open VMs", "page", "vms"))
    return out


def disk_alerts(storage, limits):
    out = []
    if not storage:
        return out
    for d in storage.get("disks", []):
        s = d.get("smart") or {}
        name = f"{d.get('model') or 'Disk'} ({d['path']})"
        key = d.get("serial") or d["path"]
        if s.get("passed") is False:
            out.append(_alert(f"smart:{key}", "bad", f"{name} is failing (SMART)",
                              "Copy its data elsewhere and replace it soon.", "Open disks", "storage", "disks"))
            continue
        bad = sum(int(s.get(k) or 0) for k in ("reallocated", "pending", "media_errors"))
        if bad > 0:
            out.append(_alert(f"sectors:{key}", "warn", f"{name} has {bad} bad sector{'s' if bad != 1 else ''}",
                              "Disks with growing numbers of bad sectors fail soon. Plan to replace it.",
                              "Open disks", "storage", "disks"))
        temp = s.get("temperature")
        limit = limits["nvme_temp_c"] if s.get("nvme") else limits["hdd_temp_c"]
        if temp is not None and not s.get("asleep") and temp > limit:
            out.append(_alert(f"temp:{key}", "warn", f"{name} is at {temp} °C (limit {limit} °C)",
                              "Check the fans and the airflow around it.", "Settings", "page", "settings"))
    for e in storage.get("fstab", []):
        if e.get("kind") == "disk" and e.get("present") is False:
            out.append(_alert(f"missing:{e['label']}", "bad", f"The {e['role']} disk {e['label']} is missing",
                              "Shares on it are offline. Check its cable and power.", "Open disks", "storage", "disks"))
    return out


def space_alerts(space):
    out = []
    for key, label in (("pool", "The pool"), ("system", "The system disk")):
        s = (space or {}).get(key)
        if s and s.get("total") and s["used"] / s["total"] > FULL:
            pct = round(100 * s["used"] / s["total"])
            out.append(_alert(f"full:{key}", "warn", f"{label} is {pct} % full",
                              f"{_size(s['total'] - s['used'])} free.", "Open storage", "storage", "shares"))
    return out


def parity_alerts(parity, now):
    if not parity or not parity.get("configured") or parity.get("running"):
        return []
    last = parity.get("last") or {}
    out = []
    if last.get("last_sync_ok") is False:
        out.append(_alert("parity:failed", "bad", "The last parity sync failed", last.get("last_sync_message"),
                          "Open pool", "storage", "pool"))
    else:
        age = _age(last.get("last_sync"), now) if last.get("last_sync") else None
        if age is None or age > PARITY_MAX_AGE:
            when = "never" if age is None else f"{age / 3600:.0f} hours ago"
            out.append(_alert("parity:old", "warn", f"Parity was last synced {when}",
                              "Changes since then can't be rebuilt if a disk fails.", "Sync now", "sync-parity", None))
    unprotected = (parity.get("diff") or {}).get("bytes") or 0
    if unprotected > UNPROTECTED_MAX:
        out.append(_alert("parity:unprotected", "warn", f"{_size(unprotected)} is not protected by parity yet",
                          "It is protected after the next sync.", "Sync now", "sync-parity", None))
    return out


def unit_alerts(units):
    return [_alert(f"unit:{u}", "bad", f"The service {u} has failed",
                   f"Look at its log: journalctl -u {u}", "Open settings" if u.startswith(("smbd", "wsdd2")) else None,
                   "page", "settings") for u in units]


def update_alerts(status, now):
    if not status:
        return []
    out = []
    counts = status.get("counts") or {}
    if counts.get("security"):
        n = counts["security"]
        out.append(_alert("updates:security", "bad", f"{n} security update{'s are' if n != 1 else ' is'} waiting",
                          None, "Install updates", "page", "updates"))
    if status.get("update_available"):
        version = (status.get("latest") or {}).get("version")
        out.append(_alert("updates:release", "info", f"NimbusOS {version} is available" if version
                          else "A new NimbusOS release is available", None, "See what's new", "page", "updates"))
    if status.get("reboot_required"):
        out.append(_alert("updates:reboot", "warn", "Restart to finish installing updates", None,
                          "Open updates", "page", "updates"))
    repo = [e for e in status.get("errors") or [] if e.get("source") == "nimbusos"]
    if repo:
        out.append(_alert("updates:repo", "warn", "The NimbusOS update repository can't be checked",
                          repo[0].get("hint") or repo[0].get("kind"), "Open updates", "page", "updates"))
    age = _age(status.get("checked_at"), now) if status.get("checked_at") else None
    if age is None or age > CHECK_MAX_AGE:
        out.append(_alert("updates:stale", "warn", "Updates haven't been checked for more than 7 days"
                          if age is not None else "Updates have never been checked", None, "Check now", "page", "updates"))
    if status.get("desktop_metapackage_installed") is False:
        out.append(_alert("updates:desktop", "warn", "The nimbusos-desktop package was removed",
                          "Without it, new NimbusOS software won't arrive. Reinstall it: sudo apt install nimbusos-desktop",
                          None))
    return out


def evaluate(*, apps=(), restart_loops=None, vms=None, storage=None, space=None, units=(), updates=None,
             limits=None, now=None):
    now = now if now is not None else datetime.datetime.now(datetime.UTC).timestamp()
    limits = limits or {"hdd_temp_c": 50, "nvme_temp_c": 70}
    found = (app_alerts(apps, restart_loops or {}) + vm_alerts(vms or {}) + disk_alerts(storage, limits)
             + space_alerts(space) + parity_alerts((storage or {}).get("parity"), now) + unit_alerts(units)
             + update_alerts(updates, now))
    return sorted(found, key=lambda a: ORDER[a["severity"]])
