# =============================================================================
# control-center/tests/test_privd.py: tests for nimbus-privd
# =============================================================================
# What: runs the real privd Server on a temporary Unix socket and talks to it
#       with the real PrivdClient:
#         - a caller with another uid is refused (SO_PEERCRED), even though it
#           can open the socket;
#         - unknown verbs, a broken request line, undeclared, missing and
#           invalid arguments are rejected with the field named;
#         - secrets are "***" in the audit log, never the value;
#         - log lines stream before the result; a verb's lock makes a second
#           caller "busy"; CommandFailed and crashes become clean errors;
#       and the update verbs with the program runner replaced:
#         - status/channel/policy/history call nimbus-update with --json and a
#           fixed argument list; bad values never reach it;
#         - install starts nimbus-update as a transient systemd unit (no shell),
#           and refuses while another one runs;
#         - follow streams the unit's journal and reports its exit status;
#       and the stack verbs on a temporary stacks folder:
#         - prepare_volume makes the folder and hands only it to the
#           container's user; delete_data removes one stack folder;
#         - neither follows a symbolic link: not one planted inside a stack,
#           not a stack that is a link, not the stacks folder swapped for one.
# Why:  docs/PLAN.md §4.3, NOTE-15, NOTE-31, NOTE-35.
# =============================================================================

import asyncio
import os
import socket
import subprocess
import time

import pytest

from nimbus_common import protocol
from nimbus_common.validate import smb_user, stack_name
from nimbus_control.privd_client import PrivdClient, PrivdError
from nimbus_privd import audit, runner, stacks, updates
from nimbus_privd.registry import REGISTRY, Arg, Registry
from nimbus_privd.server import Server


def serve(tmp_path, registry, allowed_uid, scenario):
    async def main():
        path = str(tmp_path / "privd.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen()
        sock.setblocking(False)
        task = asyncio.create_task(Server(registry, allowed_uid, idle_seconds=3600).serve(sock))
        try:
            return await scenario(PrivdClient(path), path)
        finally:
            task.cancel()
    return asyncio.run(main())


@pytest.fixture
def test_registry():
    reg = Registry()

    @reg.verb("test.echo", args={"name": Arg(stack_name)})
    def echo(ctx, args):
        return {"name": args["name"]}

    @reg.verb("test.secret", args={"user": Arg(smb_user), "password": Arg(lambda v, f: v)}, secret=("password",))
    def secret(ctx, args):
        return {"user": args["user"]}

    @reg.verb("test.stream", stream=True)
    def stream(ctx, args):
        ctx.log("one")
        ctx.log("two")
        return 2

    @reg.verb("test.slow", lock="slow")
    def slow(ctx, args):
        time.sleep(0.4)
        return "done"

    @reg.verb("test.boom")
    def boom(ctx, args):
        raise RuntimeError("kaputt")

    @reg.verb("test.fail")
    def fail(ctx, args):
        raise runner.CommandFailed(["/usr/bin/false"], 1, "line 1\nthe disk is busy\n")

    return reg


@pytest.fixture
def audit_log(monkeypatch):
    entries = []
    monkeypatch.setattr(audit, "log", lambda priority, message, **fields: entries.append((message, fields)))
    return entries


def call(tmp_path, registry, verb, args=None, uid=None):
    async def scenario(client, path):
        return await client.call(verb, args)
    return serve(tmp_path, registry, os.getuid() if uid is None else uid, scenario)


def test_ok_and_validated_arguments(tmp_path, test_registry, audit_log):
    assert call(tmp_path, test_registry, "test.echo", {"name": "jellyfin"}) == {"name": "jellyfin"}
    assert any("test.echo requested" in m for m, _ in audit_log)


@pytest.mark.parametrize("args, field, text", [
    ({"name": "../../etc"}, "name", "App names may use"),
    ({}, "name", "Missing argument"),
    ({"name": "ok", "extra": 1}, "extra", "Unexpected argument"),
])
def test_bad_arguments(tmp_path, test_registry, audit_log, args, field, text):
    with pytest.raises(PrivdError) as err:
        call(tmp_path, test_registry, "test.echo", args)
    assert err.value.code == "invalid_argument" and err.value.field == field and text in err.value.message


def test_unknown_verb(tmp_path, test_registry, audit_log):
    with pytest.raises(PrivdError) as err:
        call(tmp_path, test_registry, "disk.format", {"device": "/dev/sda"})
    assert err.value.code == "unknown_verb"


def test_other_uid_is_refused(tmp_path, test_registry, audit_log):
    with pytest.raises(PrivdError) as err:
        call(tmp_path, test_registry, "test.echo", {"name": "x"}, uid=os.getuid() + 1)
    assert err.value.code == "denied"
    assert any("refused a connection" in m for m, _ in audit_log)
    assert not any("requested" in m for m, _ in audit_log)


def test_broken_request_line(tmp_path, test_registry, audit_log):
    async def scenario(client, path):
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b"this is not json\n")
        await writer.drain()
        reply = protocol.decode(await reader.readline())
        writer.close()
        return reply
    reply = serve(tmp_path, test_registry, os.getuid(), scenario)
    assert reply["ok"] is False and reply["error"]["code"] == "invalid_request"


def test_secrets_are_redacted(tmp_path, test_registry, audit_log):
    call(tmp_path, test_registry, "test.secret", {"user": "alice", "password": "Tr0ub4dor&3"})
    logged = repr(audit_log)
    assert "Tr0ub4dor&3" not in logged
    requested = [f for m, f in audit_log if "requested" in m][0]
    assert requested["args"] == {"user": "alice", "password": "***"}


def test_stream_then_result(tmp_path, test_registry, audit_log):
    async def scenario(client, path):
        return [item async for item in client.stream("test.stream")]
    assert serve(tmp_path, test_registry, os.getuid(), scenario) == [("log", "one"), ("log", "two"), ("result", 2)]


def test_lock_makes_second_caller_busy(tmp_path, test_registry, audit_log):
    async def scenario(client, path):
        first = asyncio.create_task(client.call("test.slow"))
        await asyncio.sleep(0.1)
        with pytest.raises(PrivdError) as err:
            await client.call("test.slow")
        return await first, err.value.code
    assert serve(tmp_path, test_registry, os.getuid(), scenario) == ("done", "busy")


def test_failures_become_clean_errors(tmp_path, test_registry, audit_log):
    with pytest.raises(PrivdError) as err:
        call(tmp_path, test_registry, "test.fail")
    assert err.value.code == "failed" and "the disk is busy" in err.value.message
    with pytest.raises(PrivdError) as err:
        call(tmp_path, test_registry, "test.boom")
    assert err.value.code == "internal" and "kaputt" not in err.value.message
    assert any("internal error" in m for m, _ in audit_log)


def test_runner_needs_absolute_paths():
    with pytest.raises(ValueError):
        runner.run(["ls", "/"])
    with pytest.raises(ValueError):
        runner.run(["/bin/echo", 5])
    assert runner.run(["/bin/echo", "a;b && c"]).stdout == "a;b && c\n"    # no shell


# --- update verbs ------------------------------------------------------------------------
class FakeRunner:
    def __init__(self, outputs):
        self.outputs = outputs          # list of (prefix tuple, stdout or callable)
        self.argvs = []

    def run(self, argv, *, input=None, timeout=60, check=True, env=None):
        self.argvs.append(list(argv))
        for prefix, out in self.outputs:
            if tuple(argv[:len(prefix)]) == prefix:
                out = out() if callable(out) else out
                return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def fake_runner(monkeypatch):
    def install(outputs):
        fake = FakeRunner(outputs)
        monkeypatch.setattr(runner, "run", fake.run)
        return fake
    return install


def test_update_status_and_settings(tmp_path, audit_log, fake_runner):
    fake = fake_runner([((updates.NIMBUS_UPDATE,), '{"channel": "beta"}')])
    assert call(tmp_path, REGISTRY, "update.status") == {"channel": "beta"}
    call(tmp_path, REGISTRY, "update.set_channel", {"channel": "beta"})
    call(tmp_path, REGISTRY, "update.set_policy", {"policy": "notify"})
    call(tmp_path, REGISTRY, "update.history", {"limit": 5})
    assert fake.argvs == [
        [updates.NIMBUS_UPDATE, "--json", "status"],
        [updates.NIMBUS_UPDATE, "--json", "channel", "beta"],
        [updates.NIMBUS_UPDATE, "--json", "policy", "notify"],
        [updates.NIMBUS_UPDATE, "--json", "history", "--limit", "5"],
    ]
    for verb, args in (("update.set_channel", {"channel": "nightly; rm -rf /"}), ("update.set_policy", {"policy": "always"}),
                       ("update.history", {"limit": 10_000})):
        with pytest.raises(PrivdError) as err:
            call(tmp_path, REGISTRY, verb, args)
        assert err.value.code == "invalid_argument"
    assert len(fake.argvs) == 4


def test_update_check_keeps_the_document_when_nimbus_update_exits_1(tmp_path, audit_log, monkeypatch):
    # A failed repository makes "nimbus-update check" exit 1, and its JSON says
    # which repository and how to fix it; that must reach the Updates page.
    doc = '{"ok": false, "errors": [{"source": "nimbusos", "kind": "signature", "hint": "Reinstall the keyring."}]}'
    monkeypatch.setattr(runner, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, doc, ""))
    assert call(tmp_path, REGISTRY, "update.check")["errors"][0]["kind"] == "signature"
    monkeypatch.setattr(runner, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 2, "", "Traceback ...\nBoom\n"))
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "update.check")
    assert err.value.code == "failed" and "Boom" in err.value.message


def test_update_install_starts_a_transient_unit(tmp_path, audit_log, fake_runner):
    fake = fake_runner([((updates.SYSTEMCTL, "list-units"), ""), ((updates.SYSTEMD_RUN,), "")])
    result = call(tmp_path, REGISTRY, "update.install")
    run = [a for a in fake.argvs if a[0] == updates.SYSTEMD_RUN][0]
    assert run == [updates.SYSTEMD_RUN, f"--unit=nimbus-update-{result['job']}.service",
                   "--description=NimbusOS updates (Control Center)", "--property=RemainAfterExit=yes",
                   "--quiet", updates.NIMBUS_UPDATE, "install"]
    assert len(result["job"]) == 12


def test_update_install_refuses_while_one_runs(tmp_path, audit_log, fake_runner):
    fake_runner([((updates.SYSTEMCTL, "list-units"), "nimbus-update-aaaaaaaaaaaa.service loaded active running x\n")])
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "update.install")
    assert err.value.code == "busy" and "already being installed" in err.value.message


def test_update_install_waits_for_a_scheduled_run(tmp_path, audit_log, fake_runner):
    fake_runner([((updates.SYSTEMCTL, "list-units"),
                  "nimbus-update-check.service loaded activating start Check for NimbusOS and Debian updates\n")])
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "update.install")
    assert err.value.code == "busy" and "scheduled update run is in progress" in err.value.message


def test_update_install_ignores_a_unit_waiting_to_retry(tmp_path, audit_log, fake_runner):
    # NOTE-72: nimbus-update-auto.service waits 30 minutes after a busy exit,
    # "activating" to systemd; that must not refuse "Install updates".
    fake = fake_runner([((updates.SYSTEMCTL, "list-units"),
                         "nimbus-update-auto.service loaded activating auto-restart Install NimbusOS updates\n"),
                        ((updates.SYSTEMD_RUN,), "")])
    assert len(call(tmp_path, REGISTRY, "update.install")["job"]) == 12
    assert any(a[0] == updates.SYSTEMD_RUN for a in fake.argvs)


def test_update_follow_streams_until_the_unit_finishes(tmp_path, audit_log, fake_runner, monkeypatch):
    monkeypatch.setattr(updates, "POLL_SECONDS", 0.01)
    states = iter(["LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n"] * 3
                  + ["LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"] * 20)
    fake = fake_runner([((updates.SYSTEMCTL, "show"), lambda: next(states))])

    class Journal:
        stdout = iter(["==> apt-get update\n", "Hit:1 http://deb.debian.org/debian trixie InRelease\n"])
        terminated = False

        def terminate(self):
            Journal.terminated = True

    started = []
    monkeypatch.setattr(runner, "start", lambda argv, env=None: started.append(argv) or Journal())

    async def scenario(client, path):
        return [item async for item in client.stream("update.follow", {"job": "0123456789ab"})]
    items = serve(tmp_path, REGISTRY, os.getuid(), scenario)
    assert items[:2] == [("log", "==> apt-get update"), ("log", "Hit:1 http://deb.debian.org/debian trixie InRelease")]
    assert items[-1] == ("result", {"job": "0123456789ab", "exit_status": 0, "ok": True, "result": "success"})
    assert started == [[updates.JOURNALCTL, "--unit=nimbus-update-0123456789ab.service", "--output=cat",
                        "--no-pager", "--follow", "--lines=all"]]
    assert Journal.terminated
    assert all(a[0] == updates.SYSTEMCTL for a in fake.argvs)


def test_update_follow_unknown_job(tmp_path, audit_log, fake_runner):
    fake_runner([((updates.SYSTEMCTL, "show"), "LoadState=not-found\nActiveState=inactive\n")])
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "update.follow", {"job": "0123456789ab"})
    assert err.value.code == "invalid_argument" and "no such update run" in err.value.message


# --- stack verbs (NOTE-31) -----------------------------------------------------------------
@pytest.fixture
def stacks_root(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    (root / "web").mkdir(parents=True)
    monkeypatch.setattr(stacks, "STACKS", str(root))
    return root


def owner():
    # As root (the CI container), hand the folder to Grafana's uid; otherwise to ourselves.
    return (472, 472) if os.geteuid() == 0 else (os.getuid(), os.getgid())


def test_prepare_volume_hands_over_one_folder(tmp_path, stacks_root, audit_log):
    uid, gid = owner()
    args = {"stack": "web", "path": "data/config", "uid": uid, "gid": gid}
    result = call(tmp_path, REGISTRY, "stack.prepare_volume", args)
    assert result == {"path": f"{stacks_root}/web/data/config", "uid": uid, "gid": gid}
    made = stacks_root / "web" / "data" / "config"
    assert made.is_dir() and (made.stat().st_uid, made.stat().st_gid) == (uid, gid)
    assert (stacks_root / "web" / "data").stat().st_uid == os.geteuid()          # not recursive
    assert call(tmp_path, REGISTRY, "stack.prepare_volume", args) == result      # again: same result


@pytest.mark.parametrize("args, field", [
    ({"stack": "web", "path": "../etc", "uid": 0, "gid": 0}, "path"),
    ({"stack": "web", "path": "/etc", "uid": 0, "gid": 0}, "path"),
    ({"stack": "web", "path": "a/./b", "uid": 0, "gid": 0}, "path"),
    ({"stack": "../x", "path": "data", "uid": 0, "gid": 0}, "stack"),
    ({"stack": "web", "path": "data", "uid": 70000, "gid": 0}, "uid"),
    ({"stack": "web", "path": "data", "uid": 0, "gid": True}, "gid"),
])
def test_prepare_volume_refuses_bad_arguments(tmp_path, stacks_root, audit_log, args, field):
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "stack.prepare_volume", args)
    assert err.value.code == "invalid_argument" and err.value.field == field
    assert sorted(p.name for p in (stacks_root / "web").iterdir()) == []


def test_prepare_volume_never_follows_links(tmp_path, stacks_root, audit_log):
    uid, gid = owner()
    outside = tmp_path / "outside"
    outside.mkdir()
    (stacks_root / "web" / "data").symlink_to(outside)
    (stacks_root / "evil").symlink_to(outside)
    for stack, path in (("web", "data/config"), ("web", "data"), ("evil", "config")):
        with pytest.raises(PrivdError) as err:
            call(tmp_path, REGISTRY, "stack.prepare_volume", {"stack": stack, "path": path, "uid": uid, "gid": gid})
        assert err.value.code == "invalid_argument" and "not a plain folder" in err.value.message
    assert list(outside.iterdir()) == [] and outside.stat().st_uid == os.geteuid()


def test_delete_data_removes_one_stack_folder(tmp_path, stacks_root, audit_log):
    (stacks_root / "web" / "config").mkdir()
    (stacks_root / "web" / "config" / "db.sqlite").write_text("data")
    (stacks_root / "other").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (stacks_root / "web" / "link").symlink_to(outside)
    assert call(tmp_path, REGISTRY, "stack.delete_data", {"stack": "web"}) == {"deleted": f"{stacks_root}/web"}
    assert not (stacks_root / "web").exists() and (stacks_root / "other").is_dir()
    assert (outside / "keep").read_text() == "keep"
    assert call(tmp_path, REGISTRY, "stack.delete_data", {"stack": "web"}) == {"deleted": None}


def test_delete_data_never_follows_links(tmp_path, stacks_root, audit_log):
    victim = tmp_path / "victim"
    (victim / "etc").mkdir(parents=True)
    (victim / "etc" / "passwd").write_text("root:x:0:0")
    (stacks_root / "etc").symlink_to(victim / "etc")
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "stack.delete_data", {"stack": "etc"})
    assert err.value.code == "invalid_argument" and "not a plain folder" in err.value.message
    # The stacks folder (owned by nimbus-control) swapped for a link.
    stacks_root.rename(tmp_path / "real-stacks")
    stacks_root.symlink_to(victim)
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "stack.delete_data", {"stack": "etc"})
    assert err.value.code == "invalid_argument"
    assert (victim / "etc" / "passwd").read_text() == "root:x:0:0"
    with pytest.raises(PrivdError) as err:
        call(tmp_path, REGISTRY, "stack.delete_data", {"stack": "../etc"})
    assert err.value.field == "stack"
