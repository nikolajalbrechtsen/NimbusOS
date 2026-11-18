# =============================================================================
# control-center/tests/test_validate.py: tests for nimbus_common.validate
# =============================================================================
# What: accepted and rejected names, ports and numbers; path_under resolves
#       "..", symbolic links and look-alike prefixes before deciding.
# Why:  docs/PLAN.md §4.3; privd and the backend share these rules.
# =============================================================================

import os

import pytest

from nimbus_common import validate as v


@pytest.mark.parametrize("name", ["jellyfin", "my-app_2", "a"])
def test_stack_names_ok(name):
    assert v.stack_name(name) == name


@pytest.mark.parametrize("name", ["", "Jellyfin", "-x", "a b", "a/b", "x" * 49, "../etc", None, 5])
def test_stack_names_rejected(name):
    with pytest.raises(v.ValidationError) as err:
        v.stack_name(name)
    assert err.value.field == "name"


@pytest.mark.parametrize("name", ["media", "Time.Machine", "backups-2"])
def test_share_names_ok(name):
    assert v.share_name(name) == name


@pytest.mark.parametrize("name", ["global", "Homes", "IPC$", "print$", "a b", ".hidden", "x/y", "x" * 65])
def test_share_names_rejected(name):
    with pytest.raises(v.ValidationError):
        v.share_name(name)


def test_reserved_share_name_message():
    with pytest.raises(v.ValidationError, match="reserved by Samba"):
        v.share_name("printers")


@pytest.mark.parametrize("value", [1, 80, 65535])
def test_ports_ok(value):
    assert v.port(value) == value


@pytest.mark.parametrize("value", [0, 65536, -1, "80", 80.0, True, None])
def test_ports_rejected(value):
    with pytest.raises(v.ValidationError, match="1 to 65535"):
        v.port(value)


def test_smb_user_and_label():
    assert v.smb_user("alice") == "alice"
    with pytest.raises(v.ValidationError):
        v.smb_user("Alice")
    assert v.disk_label("disk1") == "disk1"
    with pytest.raises(v.ValidationError):
        v.disk_label("a-very-long-label")


def test_one_of_int_range_job_id():
    assert v.one_of("beta", ("stable", "beta"), "channel") == "beta"
    with pytest.raises(v.ValidationError) as err:
        v.one_of("nightly", ("stable", "beta"), "channel")
    assert err.value.field == "channel"
    assert v.int_range(5, 1, 10, "n") == 5
    with pytest.raises(v.ValidationError):
        v.int_range(True, 0, 10, "n")
    assert v.job_id("0123456789ab") == "0123456789ab"
    for bad in ("0123456789AB", "0123", "../../etc", None):
        with pytest.raises(v.ValidationError):
            v.job_id(bad)


def test_path_under(tmp_path):
    root = tmp_path / "stacks"
    (root / "app" / "data").mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.mkdir()
    (root / "app" / "link").symlink_to(outside)
    (tmp_path / "stacks-evil").mkdir()

    assert v.path_under(str(root / "app" / "data"), [str(root)]) == str(root / "app" / "data")
    assert v.path_under(str(root), [str(root)]) == str(root)
    for bad in (str(root / ".." / "secret"), str(root / "app" / "link"), str(tmp_path / "stacks-evil"),
                "relative/path", "/etc/passwd", f"{root}/\x00"):
        with pytest.raises(v.ValidationError):
            v.path_under(bad, [str(root)])
    with pytest.raises(v.ValidationError, match="does not exist"):
        v.path_under(str(root / "missing"), [str(root)], must_exist=True)
    assert os.path.isabs(v.path_under(str(root / "app" / "new"), [str(root)]))
