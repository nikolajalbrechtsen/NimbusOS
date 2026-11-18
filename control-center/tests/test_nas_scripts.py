# =============================================================================
# control-center/tests/test_nas_scripts.py: the storage timers' scripts
# =============================================================================
# What: - nimbus-recycle-clean: files older than a share's retention leave
#         its .recycle, newer ones stay, emptied folders go; a link in the
#         bin is removed itself, never followed; shares without a recycle
#         bin, on a missing disk, or not valid are left alone;
#       - nimbus-snapraid-run: nothing without a configuration; a sync, then
#         the weekly scrub the first time and not again within a week; a
#         failed sync is recorded, exits non-zero and skips the scrub; the
#         cached diff counts are dropped after a run.
#       (nimbus-netfix runs against real iptables in
#       tests/integration/nas-integration.sh.)
# Why:  spec Tab C (recycle bin retention, scheduled parity); NOTE-26, NOTE-68.
# =============================================================================

import importlib.machinery
import importlib.util
import json
import os
import time

import pytest

from conftest import CC
from nimbus_common import shares as S

DAY = 86400


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(CC / "bin" / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


# --- nimbus-recycle-clean ------------------------------------------------------------------------
@pytest.fixture
def recycle(tmp_path, monkeypatch):
    mod = load("nimbus-recycle-clean")
    for name, sub in (("DISKS", "mnt/disks"), ("POOL", "mnt/user"), ("SYSTEM_SHARES", "srv/shares")):
        (tmp_path / sub).mkdir(parents=True)
        monkeypatch.setattr(S, name, str(tmp_path / sub))
    state = tmp_path / "shares.json"
    monkeypatch.setattr(mod, "STATE", str(state))
    return mod, tmp_path, state


def aged(path, days, link=False):
    t = time.time() - days * DAY
    os.utime(path, (t, t), follow_symlinks=not link)


def test_recycle_clean(recycle):
    mod, tmp, state = recycle
    bin_ = tmp / "srv/shares/media/.recycle"
    (bin_ / "alice/old-folder").mkdir(parents=True)
    (bin_ / "alice/old-folder/a.mkv").write_text("old")
    aged(bin_ / "alice/old-folder/a.mkv", 10)
    (bin_ / "alice/new.txt").write_text("new")
    (tmp / "precious").write_text("keep")
    aged(tmp / "precious", 100)
    os.symlink(tmp / "precious", bin_ / "link")
    aged(bin_ / "link", 10, link=True)
    (tmp / "srv/shares/media/live.txt").write_text("not in the bin")
    aged(tmp / "srv/shares/media/live.txt", 100)
    (tmp / "srv/shares/docs/.recycle").mkdir(parents=True)
    (tmp / "srv/shares/docs/.recycle/old.txt").write_text("x")
    aged(tmp / "srv/shares/docs/.recycle/old.txt", 10)
    state.write_text(json.dumps({"shares": [
        {"name": "media", "location": "system", "recycle": {"enabled": True, "days": 7}},
        {"name": "docs", "location": "system", "recycle": {"enabled": False, "days": 7}},
        {"name": "films", "location": "disk:disk9", "recycle": {"enabled": True, "days": 1}},
        {"name": "../evil", "location": "system", "recycle": {"enabled": True, "days": 1}}]}))
    assert mod.main() == 0
    assert not (bin_ / "alice/old-folder").exists() and (bin_ / "alice/new.txt").read_text() == "new"
    assert not os.path.lexists(bin_ / "link") and (tmp / "precious").read_text() == "keep"
    assert (tmp / "srv/shares/media/live.txt").exists() and (tmp / "srv/shares/docs/.recycle/old.txt").exists()


def test_recycle_clean_without_state(recycle):
    mod, tmp, state = recycle
    assert mod.main() == 0


# --- nimbus-snapraid-run ------------------------------------------------------------------------------
@pytest.fixture
def snapraid(tmp_path, monkeypatch):
    mod = load("nimbus-snapraid-run")
    fake = tmp_path / "snapraid"
    fake.write_text('#!/bin/sh\necho "$@" >> "$0.log"\ncase "$3" in sync) echo "Everything OK"; exit ${SYNC_RC:-0};;'
                    ' scrub) echo "Scrub done"; exit 0;; esac\n')
    fake.chmod(0o755)
    monkeypatch.setattr(mod, "SNAPRAID", str(fake))
    monkeypatch.setattr(mod, "CONF", str(tmp_path / "snapraid.conf"))
    monkeypatch.setattr(mod, "STATUS", str(tmp_path / "cache/snapraid-status.json"))
    return mod, tmp_path


def calls(tmp):
    return [line.split()[2] for line in (tmp / "snapraid.log").read_text().splitlines()]


def test_snapraid_run(snapraid, monkeypatch):
    mod, tmp = snapraid
    assert mod.main() == 0 and not (tmp / "snapraid.log").exists()                   # not set up
    (tmp / "snapraid.conf").write_text("# conf\n")
    (tmp / "cache").mkdir()
    (tmp / "cache/snapraid-diff.json").write_text("{}")
    assert mod.main() == 0 and calls(tmp) == ["sync", "scrub"]
    status = json.loads((tmp / "cache/snapraid-status.json").read_text())
    assert status["last_sync_ok"] is True and status["last_sync_message"] == "Everything OK" and status["last_scrub_ok"] is True
    assert not (tmp / "cache/snapraid-diff.json").exists()
    assert mod.main() == 0 and calls(tmp) == ["sync", "scrub", "sync"]                 # scrub weekly only
    monkeypatch.setenv("SYNC_RC", "1")
    monkeypatch.setattr(mod, "snapraid", lambda *a: (1, "Error: missing disk disk2") if a[0] == "sync" else (0, ""))
    assert mod.main() == 1
    status = json.loads((tmp / "cache/snapraid-status.json").read_text())
    assert status["last_sync_ok"] is False and status["last_sync_message"] == "Error: missing disk disk2"


def test_snapraid_scrubs_again_after_a_week(snapraid):
    mod, tmp = snapraid
    (tmp / "snapraid.conf").write_text("# conf\n")
    (tmp / "cache").mkdir()
    (tmp / "cache/snapraid-status.json").write_text(json.dumps({"last_scrub": "2026-11-01T03:30:00+00:00"}))
    mod.main()
    assert calls(tmp) == ["sync", "scrub"]
