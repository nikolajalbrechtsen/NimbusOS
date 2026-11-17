"""Unit tests for nimbus-update (packages/nimbusos-release/nimbus-update/nimbus-update).

What: checks the parts of nimbus-update that decide what users are told and
what gets written: apt error classification (one message per problem, the
right source and kind, harmless warnings ignored), update.conf reading and
writing, the beta source, origin grouping, release selection per channel,
apt history parsing, and `status --json` on a system that was never checked.

Why: a wrong classification would show a bad signature as "no updates"
(spec 4.4), and a wrong conf write would silently change the user's channel.

Run: python3 -m pytest packages/nimbusos-release/tests (Debian: python3-pytest;
the release-selection test also needs python3-apt).
"""

import importlib.machinery
import importlib.util
import json
import pathlib
import types

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "nimbus-update" / "nimbus-update"
NIMBUS_HOST = "nikolajalbrechtsen.github.io"


@pytest.fixture()
def nu(tmp_path):
    """nimbus-update loaded as a module, with build values and paths set for the test."""
    loader = importlib.machinery.SourceFileLoader("nimbus_update_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.APT_HOST = NIMBUS_HOST
    module.APT_URL = f"https://{NIMBUS_HOST}/NimbusOS/apt"
    module.APT_ORIGIN = "NimbusOS"
    module.APT_COMPONENT = "main"
    module.DOCKER_APT_HOST = "download.docker.com"
    module.ARCHIVE_KEYRING = "/usr/share/keyrings/nimbusos-archive-keyring.gpg"
    module.BUG_REPORT_URL = "https://example.invalid/issues"
    module.CONF_PATH = str(tmp_path / "update.conf")
    module.CONF_DEFAULT = str(tmp_path / "update.conf.default")
    module.STATUS_PATH = str(tmp_path / "update-status.json")
    module.OS_RELEASE = str(tmp_path / "os-release")
    module.REBOOT_REQUIRED = str(tmp_path / "reboot-required")
    module.REBOOT_REQUIRED_PKGS = str(tmp_path / "reboot-required.pkgs")
    return module


# --- apt error classification --------------------------------------------------

def classify(nu, text):
    return nu.classify_apt_output(text.splitlines(keepends=True))


def test_dns_failure_is_reported_once_for_nimbusos(nu):
    errors = classify(nu, f"""\
Hit:1 http://deb.debian.org/debian trixie InRelease
Err:2 https://{NIMBUS_HOST}/NimbusOS/apt stable InRelease
  Temporary failure resolving '{NIMBUS_HOST}'
W: Failed to fetch https://{NIMBUS_HOST}/NimbusOS/apt/dists/stable/InRelease  Temporary failure resolving '{NIMBUS_HOST}'
W: Some index files failed to download. They have been ignored, or old ones used instead.
""")
    assert len(errors) == 1
    assert errors[0]["source"] == "nimbusos"
    assert errors[0]["kind"] == "dns"
    assert NIMBUS_HOST in errors[0]["hint"]


def test_bad_signature_is_an_error_not_no_updates(nu):
    errors = classify(nu, f"""\
Get:1 https://{NIMBUS_HOST}/NimbusOS/apt stable InRelease [2345 B]
Err:1 https://{NIMBUS_HOST}/NimbusOS/apt stable InRelease
  Sub-process /usr/bin/sqv returned an error code (1), error message is: Missing key 0123456789ABCDEF0123456789ABCDEF01234567, which is needed to verify signature.
W: OpenPGP signature verification failed: https://{NIMBUS_HOST}/NimbusOS/apt stable InRelease: Sub-process /usr/bin/sqv returned an error code (1)
E: The repository 'https://{NIMBUS_HOST}/NimbusOS/apt stable InRelease' is not signed.
""")
    assert [(e["source"], e["kind"]) for e in errors] == [("nimbusos", "signature")]
    assert "nimbusos-archive-keyring" in errors[0]["hint"]


def test_expired_debian_metadata(nu):
    errors = classify(nu, "E: Release file for http://deb.debian.org/debian/dists/trixie/InRelease is expired "
                          "(invalid since 2d 3h 4min 5s). Updates for this repository will not be applied.\n")
    assert [(e["source"], e["kind"]) for e in errors] == [("debian", "expired")]


def test_tls_problem_with_docker(nu):
    errors = classify(nu, "W: Failed to fetch https://download.docker.com/linux/debian/dists/trixie/InRelease  "
                          "Certificate verification failed: The certificate is NOT trusted.\n")
    assert [(e["source"], e["kind"]) for e in errors] == [("docker", "tls")]


def test_missing_release_file_is_unreachable(nu):
    errors = classify(nu, f"E: The repository 'https://{NIMBUS_HOST}/NimbusOS/apt beta Release' "
                          "does not have a Release file.\n")
    assert [(e["source"], e["kind"]) for e in errors] == [("nimbusos", "unreachable")]


def test_harmless_warnings_are_ignored(nu):
    errors = classify(nu, "W: Target Packages (main/binary-amd64/Packages) is configured multiple times in "
                          "/etc/apt/sources.list.d/a.list:1 and /etc/apt/sources.list.d/b.list:1\n")
    assert errors == []


# --- update.conf -----------------------------------------------------------------

def test_conf_defaults_and_invalid_values(nu):
    assert nu.read_conf() == {"CHANNEL": "stable", "POLICY": "automatic"}
    pathlib.Path(nu.CONF_PATH).write_text('# comment\nCHANNEL="beta"\nPOLICY=sometimes\n')
    assert nu.read_conf() == {"CHANNEL": "beta", "POLICY": "automatic"}


def test_conf_write_keeps_comments_and_other_keys(nu):
    pathlib.Path(nu.CONF_PATH).write_text("# keep me\nCHANNEL=stable\nPOLICY=notify\n")
    nu.write_conf_value("CHANNEL", "beta")
    text = pathlib.Path(nu.CONF_PATH).read_text()
    assert text == "# keep me\nCHANNEL=beta\nPOLICY=notify\n"
    assert nu.read_conf() == {"CHANNEL": "beta", "POLICY": "notify"}


def test_conf_write_starts_from_packaged_default(nu):
    pathlib.Path(nu.CONF_DEFAULT).write_text("# default\nCHANNEL=stable\nPOLICY=automatic\n")
    nu.write_conf_value("POLICY", "off")
    assert pathlib.Path(nu.CONF_PATH).read_text() == "# default\nCHANNEL=stable\nPOLICY=off\n"


def test_beta_source_uses_the_archive_keyring(nu):
    text = nu.beta_sources_text()
    assert "Suites: beta\n" in text
    assert f"URIs: https://{NIMBUS_HOST}/NimbusOS/apt/\n" in text
    assert "Signed-By: /usr/share/keyrings/nimbusos-archive-keyring.gpg\n" in text


# --- grouping and releases -------------------------------------------------------

def origin(**fields):
    defaults = {"label": "Debian", "origin": "Debian", "site": "deb.debian.org"}
    defaults.update(fields)
    return types.SimpleNamespace(**defaults)


def test_origin_groups(nu):
    assert nu.classify_origin([origin(label="Debian-Security", site="security.debian.org")]) == "security"
    assert nu.classify_origin([origin(label="NimbusOS", origin="NimbusOS", site=NIMBUS_HOST)]) == "nimbusos"
    assert nu.classify_origin([origin(origin="Docker", label="Docker CE", site="download.docker.com")]) == "debian"
    assert nu.classify_origin([origin()]) == "debian"


def test_latest_release_per_channel(nu):
    apt_pkg = pytest.importorskip("apt_pkg")
    apt_pkg.init_system()
    releases = {"releases": [
        {"version": "0.1.0", "deb_version": "0.1.0", "channel": "stable"},
        {"version": "0.2.0-beta.1", "deb_version": "0.2.0~beta.1", "channel": "beta"},
        {"version": "0.1.1", "deb_version": "0.1.1", "channel": "stable"},
        {"version": "0.2.0-beta.2", "deb_version": "0.2.0~beta.2", "channel": "beta"},
    ]}
    compare = apt_pkg.version_compare
    assert nu.latest_release(releases, "stable", compare)["version"] == "0.1.1"
    assert nu.latest_release(releases, "beta", compare)["version"] == "0.2.0-beta.2"
    releases["releases"].append({"version": "0.2.0", "deb_version": "0.2.0", "channel": "stable"})
    assert nu.latest_release(releases, "beta", compare)["version"] == "0.2.0"


# --- history ---------------------------------------------------------------------

HISTORY = """\

Start-Date: 2026-11-16  10:00:01
Commandline: apt-get -y -q full-upgrade
Requested-By: admin (1000)
Upgrade: nimbus-control:all (0.1.0, 0.1.1), libfoo1:amd64 (1.0-1, 1.0-2)
Install: libbar2:amd64 (2.0-1, automatic)
End-Date: 2026-11-16  10:00:30

Start-Date: 2026-11-17  09:00:00
Commandline: apt-get install htop
Install: htop:amd64 (3.4.0-2)
Error: Sub-process /usr/bin/dpkg returned an error code (1)
End-Date: 2026-11-17  09:00:05
"""


def test_history_parsing(nu):
    entries = nu.parse_history(HISTORY)
    assert [e["start"] for e in entries] == ["2026-11-16  10:00:01", "2026-11-17  09:00:00"]
    first = entries[0]["changes"]
    assert first["upgrade"][0] == {"name": "nimbus-control", "versions": ["0.1.0", "0.1.1"], "automatic": False}
    assert first["install"][0] == {"name": "libbar2", "versions": ["2.0-1"], "automatic": True}
    assert entries[1]["error"].startswith("Sub-process")


# --- Recommends always reach the system (docs/PLAN.md NOTE-48) --------------------

def capture_apt_calls(nu, monkeypatch):
    """Run install/auto with every side effect replaced; return the apt-get argv lists."""
    calls = []
    monkeypatch.setattr(nu, "require_root", lambda: None)
    monkeypatch.setattr(nu, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(nu, "lock_is_free", lambda path: True)
    monkeypatch.setattr(nu, "apt_update", lambda lock_timeout: [])
    monkeypatch.setattr(nu, "refresh_status", lambda errors, ts: {"reboot_required": False, "ok": True})
    monkeypatch.setattr(nu, "installed_nimbus_packages", lambda: ["nimbusos-desktop"])
    monkeypatch.setattr(nu, "run_streaming", lambda argv, env: (calls.append(argv), (0, []))[1])
    monkeypatch.setattr(nu.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="", returncode=0))
    return calls


@pytest.mark.parametrize("command", ["install", "auto"])
def test_updates_install_new_recommends(nu, monkeypatch, command):
    calls = capture_apt_calls(nu, monkeypatch)
    assert nu.main([command]) == 0
    assert len(calls) == 1
    argv = calls[0]
    assert "--no-install-recommends" not in argv
    assert "APT::Install-Recommends=true" in argv
    assert "Dpkg::Options::=--force-confold" in argv


def test_status_warns_when_the_metapackage_is_missing(nu, monkeypatch, capsys):
    monkeypatch.setattr(nu, "package_installed", lambda name: False)
    assert nu.main(["status"]) == 0
    assert "This system no longer receives new NimbusOS software. Reinstall nimbusos-desktop" in capsys.readouterr().out
    monkeypatch.setattr(nu, "package_installed", lambda name: True)
    assert nu.main(["status"]) == 0
    assert "no longer receives" not in capsys.readouterr().out


# --- status on a fresh system ----------------------------------------------------

def test_status_json_before_first_check(nu, capsys):
    pathlib.Path(nu.OS_RELEASE).write_text(
        'NAME="NimbusOS"\nPRETTY_NAME="NimbusOS 0.1.0"\nVERSION_ID="0.1.0"\n'
        'NIMBUSOS_CHANNEL=stable\nNIMBUSOS_DEB_VERSION="0.1.0"\n')
    pathlib.Path(nu.REBOOT_REQUIRED).write_text("*** System restart required ***\n")
    pathlib.Path(nu.REBOOT_REQUIRED_PKGS).write_text("linux-image-amd64\n")
    nu.package_installed = lambda name: True
    assert nu.main(["--json", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["checked_at"] is None
    assert status["installed"]["deb_version"] == "0.1.0"
    assert status["channel"] == "stable" and status["policy"] == "automatic"
    assert status["reboot_required"] is True
    assert status["reboot_packages"] == ["linux-image-amd64"]
    assert status["update_available"] is False
    assert status["desktop_metapackage_installed"] is True
