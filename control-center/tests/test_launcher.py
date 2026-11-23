"""Tests for control-center/bin/nimbus-control-open (the app menu launcher).

What: the Chromium command line the launcher replaces itself with: app mode
on the one-time launch code, the window class, a profile of its own, and
--password-store=basic, so Plasma's KDE Wallet never asks to create a wallet
(found in the QEMU test: its dialogs held the page until the launch code had
expired). The token itself is never on the command line.

Why: docs/PLAN.md NOTE-14 and NOTE-72.
"""

import importlib.machinery
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "bin" / "nimbus-control-open"


@pytest.fixture()
def launcher():
    loader = importlib.machinery.SourceFileLoader("nimbus_control_open_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_chromium_command_line(launcher, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(launcher, "session_has_access", lambda: True)
    monkeypatch.setattr(launcher, "launch_code", lambda: "one-time-code")
    monkeypatch.setattr(launcher.os, "execv", lambda path, argv: calls.append((path, argv)))

    launcher.main()

    assert len(calls) == 1
    path, argv = calls[0]
    assert path == launcher.CHROMIUM and argv[0] == launcher.CHROMIUM
    assert "--app=http://127.0.0.1:7777/?launch=one-time-code" in argv
    assert "--class=NimbusControl" in argv
    profile = tmp_path / "nimbus-control" / "chromium"
    assert f"--user-data-dir={profile}" in argv
    assert profile.is_dir() and (profile.stat().st_mode & 0o777) == 0o700
    assert "--password-store=basic" in argv
    assert not any("token" in arg for arg in argv)


def test_no_access_offers_grant_and_starts_nothing(launcher, monkeypatch):
    calls, offered = [], []
    monkeypatch.setattr(launcher, "session_has_access", lambda: False)
    monkeypatch.setattr(launcher, "offer_access", lambda: offered.append(True))
    monkeypatch.setattr(launcher.os, "execv", lambda path, argv: calls.append(argv))

    assert launcher.main() == 0
    assert offered == [True] and calls == []
