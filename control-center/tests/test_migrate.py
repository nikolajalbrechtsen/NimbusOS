# =============================================================================
# control-center/tests/test_migrate.py: tests for bin/nimbus-migrate
# =============================================================================
# What: runs nimbus-migrate as a program (no root, no network) against
#       temporary state directories:
#         - a fresh install reaches the newest schema with the real steps;
#         - an upgrade from a fake older version (state at schema 1, written
#           as nimbus-control 0001 leaves it) runs fake steps 2 and 3 in
#           order, one at a time, after backing up the state files;
#         - a failing step restores every state file byte for byte, keeps the
#           schema, exits 1 and prints one clear message naming the step;
#         - state newer than the newest step (a downgrade) is refused and
#           left untouched; state files without schema_version are refused;
#         - stacks/ is never touched or copied, a second run changes
#           nothing, at most 5 backups are kept, and gaps in step numbers
#           are rejected.
# Why:  docs/PLAN.md NOTE-59: upgrades must never lose or half-migrate state.
#
# Usage: python3 -m pytest control-center/tests/test_migrate.py
# =============================================================================

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

HERE = pathlib.Path(__file__).resolve().parent
MIGRATE = HERE.parent / "bin" / "nimbus-migrate"
REAL_STEPS = HERE.parent / "migrations"


def run(state, steps, *args):
    env = {k: v for k, v in os.environ.items() if k != "JOURNAL_STREAM"}
    return subprocess.run(
        [sys.executable, str(MIGRATE), "--state-dir", str(state), "--migrations-dir", str(steps), *args],
        capture_output=True, text=True, env=env, check=False)


def tree_digest(path):
    """name -> sha256 for every file under PATH (relative names)."""
    out = {}
    for p in sorted(pathlib.Path(path).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(path))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def top_files(state):
    return {p.name: p.read_bytes() for p in pathlib.Path(state).iterdir()
            if p.is_file() and not p.name.startswith(".")}


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    (d / "stacks" / "jellyfin" / "data").mkdir(parents=True)
    (d / "stacks" / "jellyfin" / "docker-compose.yml").write_text("services: {}\n")
    (d / "stacks" / "jellyfin" / "data" / "library.db").write_bytes(os.urandom(4096))
    (d / "backups").mkdir()
    return d


@pytest.fixture
def steps(tmp_path):
    """The real step 0001 plus fake steps 0002 and 0003, as a newer
    nimbus-control would ship them."""
    d = tmp_path / "steps"
    d.mkdir()
    shutil.copy(REAL_STEPS / "0001_initial.py", d)
    (d / "0002_share_ids.py").write_text(textwrap.dedent('''
        DESCRIPTION = "give every share an id"
        def migrate(ctx):
            data = ctx.read_json("shares.json")
            for i, share in enumerate(data["shares"], start=1):
                share.setdefault("id", i)
            ctx.write_json("shares.json", data)
            ctx.log("shares numbered")
    '''))
    (d / "0003_settings_theme.py").write_text(textwrap.dedent('''
        DESCRIPTION = "add the theme setting"
        def migrate(ctx):
            settings = ctx.read_json("settings.json")
            assert "share_ids_done" not in settings
            shares = ctx.read_json("shares.json")
            assert all("id" in s for s in shares["shares"]), "step 0002 must run first"
            settings["theme"] = "dark"
            ctx.write_json("settings.json", settings)
    '''))
    return d


def write_schema1_state(state):
    """State as the fake older nimbus-control (schema 1) left it."""
    (state / "schema_version").write_text("1\n")
    (state / "settings.json").write_text(json.dumps({"hostname_shown": True}) + "\n")
    (state / "shares.json").write_text(json.dumps({"shares": [{"name": "Media"}, {"name": "Backup"}]}) + "\n")
    (state / "stacks.json").write_text(json.dumps(
        {"stacks": {"jellyfin": {"app": "jellyfin", "template_version": 1}}}) + "\n")


def test_fresh_install_with_real_steps(state):
    stacks_before = tree_digest(state / "stacks")
    r = run(state, REAL_STEPS)
    assert r.returncode == 0, r.stderr
    newest = len([p for p in REAL_STEPS.glob("[0-9][0-9][0-9][0-9]_*.py")])
    assert (state / "schema_version").read_text() == f"{newest}\n"
    assert json.loads((state / "shares.json").read_text()) == {"shares": []}
    assert json.loads((state / "stacks.json").read_text()) == {"stacks": {}}
    assert json.loads((state / "settings.json").read_text()) == {}
    assert tree_digest(state / "stacks") == stacks_before
    assert "now at schema" in r.stderr


def test_upgrade_from_fake_older_version_runs_steps_in_order(state, steps):
    write_schema1_state(state)
    before = top_files(state)
    stacks_before = tree_digest(state / "stacks")
    r = run(state, steps)
    assert r.returncode == 0, r.stderr
    assert (state / "schema_version").read_text() == "3\n"
    shares = json.loads((state / "shares.json").read_text())["shares"]
    assert [s["id"] for s in shares] == [1, 2]
    settings = json.loads((state / "settings.json").read_text())
    assert settings == {"hostname_shown": True, "theme": "dark"}
    # Step by step, in order, logged.
    assert r.stderr.index("step 0002 done") < r.stderr.index("step 0003 done")
    # Backup of the state files from before the run, and only of them.
    backups = list((state / "backups").iterdir())
    assert len(backups) == 1 and backups[0].name.startswith("schema-1-")
    assert {p.name: p.read_bytes() for p in backups[0].iterdir()} == before
    assert tree_digest(state / "stacks") == stacks_before


def test_failed_step_restores_state_and_reports(state, steps):
    write_schema1_state(state)
    (steps / "0003_settings_theme.py").write_text(textwrap.dedent('''
        DESCRIPTION = "add the theme setting"
        def migrate(ctx):
            s = ctx.read_json("settings.json")
            s["half"] = "written"
            ctx.write_json("settings.json", s)
            ctx.write_json("new-file.json", {"x": 1})
            raise RuntimeError("disk on fire")
    '''))
    before = top_files(state)
    r = run(state, steps)
    assert r.returncode == 1
    # Step 0002 had already run and been recorded; everything is rolled back.
    assert top_files(state) == before
    assert (state / "schema_version").read_text() == "1\n"
    msg = [line for line in r.stderr.splitlines() if "failed" in line]
    assert len(msg) == 1
    assert "state migration 0003 (add the theme setting) failed: RuntimeError: disk on fire" in msg[0]
    assert "restored from" in msg[0] and "will not start" in msg[0]
    # A second run after fixing the step succeeds from schema 1.
    shutil.copy(REAL_STEPS / "0001_initial.py", steps)  # unchanged
    (steps / "0003_settings_theme.py").write_text('DESCRIPTION = "fixed"\ndef migrate(ctx):\n    pass\n')
    assert run(state, steps).returncode == 0
    assert (state / "schema_version").read_text() == "3\n"


def test_broken_step_file_is_reported_like_a_failure(state, steps):
    write_schema1_state(state)
    (steps / "0002_share_ids.py").write_text("this is not python\n")
    before = top_files(state)
    r = run(state, steps)
    assert r.returncode == 1
    assert top_files(state) == before
    assert "state migration 0002" in r.stderr and "SyntaxError" in r.stderr


def test_downgrade_is_refused_and_state_untouched(state):
    write_schema1_state(state)
    (state / "schema_version").write_text("7\n")
    before = top_files(state)
    r = run(state, REAL_STEPS)
    assert r.returncode == 3
    assert "newer than this nimbus-control supports" in r.stderr
    assert top_files(state) == before
    assert not list((state / "backups").iterdir())


def test_state_without_schema_version_is_refused(state):
    (state / "settings.json").write_text("{}\n")
    r = run(state, REAL_STEPS)
    assert r.returncode == 3
    assert "no schema_version" in r.stderr
    assert not (state / "schema_version").exists()


def test_second_run_changes_nothing(state, steps):
    write_schema1_state(state)
    assert run(state, steps).returncode == 0
    before = tree_digest(state)
    r = run(state, steps)
    assert r.returncode == 0 and r.stderr == ""
    assert tree_digest(state) == before


def test_at_most_five_backups_are_kept(state, steps):
    write_schema1_state(state)
    for _ in range(7):
        (state / "schema_version").write_text("1\n")
        assert run(state, steps).returncode == 0
    assert len(list((state / "backups").iterdir())) == 5


def test_gap_in_step_numbers_is_rejected(state, steps):
    write_schema1_state(state)
    (steps / "0003_settings_theme.py").rename(steps / "0004_settings_theme.py")
    before = top_files(state)
    r = run(state, steps)
    assert r.returncode == 2 and "0003 was expected" in r.stderr
    assert top_files(state) == before


def test_status(state, steps):
    write_schema1_state(state)
    r = run(state, steps, "--status")
    assert r.returncode == 0 and r.stdout.split() == ["1", "3"]
