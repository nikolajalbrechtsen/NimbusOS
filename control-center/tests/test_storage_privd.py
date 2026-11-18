# =============================================================================
# control-center/tests/test_storage_privd.py: privd's disk and pool verbs
# =============================================================================
# What: storage.py and fsutil.py with every command replaced (no real disk
#       is ever touched) and the storage folders in a temporary directory:
#         - disk roles from lsblk: the system disk behind LUKS and LVM, the
#           live medium, data and parity disks, a disk without a serial;
#           loop and zram devices left out;
#         - SMART summaries (ATA, NVMe, asleep, no SMART), the hourly cache,
#           a sleeping disk keeping its last reading;
#         - the managed fstab block: the rest of the file kept byte for byte,
#           backups (10 kept), entries read back, nothing written when
#           nothing changes;
#         - disk.format: refused for the system disk, the live medium, a
#           mounted disk, a wrong serial, a used label and in the live
#           session; otherwise the exact commands, the fstab entry, the mount;
#         - disk.mount / disk.unmount (refused while in the pool or parity);
#         - pool.apply: the mergerfs line, branches changed on a mounted pool,
#           SnapRAID's configuration, parity smaller than data refused,
#           turning parity off keeps the old configuration;
#         - snapraid diff counts and the cached status.
# Why:  spec Tab C (Disks, Pool); docs/PLAN.md §4.5, NOTE-68.
# =============================================================================

import json
import os
import subprocess

import pytest

from nimbus_common import shares as S
from nimbus_common.validate import ValidationError
from nimbus_privd import fsutil, runner, storage
from nimbus_privd.registry import REGISTRY


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


class FakeRunner:
    """Answers commands by their first arguments; a callable answer is called."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def run(self, argv, *, input=None, timeout=60, check=True, env=None):
        self.calls.append(list(argv))
        for prefix, answer in self.answers:
            if tuple(argv[:len(prefix)]) == prefix:
                out = answer(argv) if callable(answer) else answer
                rc, stdout = (out if isinstance(out, tuple) else (0, out))
                if check and rc:
                    raise runner.CommandFailed(argv, rc, stdout)
                return subprocess.CompletedProcess(argv, rc, stdout, "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("DISKS", "mnt/disks"), ("PARITY", "mnt/parity"), ("POOL", "mnt/user"),
                      ("SYSTEM_SHARES", "srv/shares"), ("ISOS", "isos")):
        (tmp_path / sub).mkdir(parents=True)
        monkeypatch.setattr(S, name, str(tmp_path / sub))
    (tmp_path / "etc").mkdir()
    monkeypatch.setattr(fsutil, "FSTAB", str(tmp_path / "etc/fstab"))
    monkeypatch.setattr(fsutil, "BACKUPS", str(tmp_path / "fstab-backups"))
    monkeypatch.setattr(storage, "SMART_CACHE", str(tmp_path / "smart"))
    monkeypatch.setattr(storage, "SNAPRAID_CONF", str(tmp_path / "etc/snapraid.conf"))
    monkeypatch.setattr(storage, "SNAPRAID_CONTENT", str(tmp_path / "privd/snapraid.content"))
    monkeypatch.setattr(storage, "SNAPRAID_STATUS", str(tmp_path / "cache/snapraid-status.json"))
    monkeypatch.setattr(storage, "SNAPRAID_DIFF", str(tmp_path / "cache/snapraid-diff.json"))
    monkeypatch.setattr(storage, "live_session", lambda: False)
    mounted = set()
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: p in mounted or real_ismount(p))
    return tmp_path, mounted


def disk(name, children=(), serial=None, model="WDC WD40EFRX", size=4_000_787_030_016, mounts=(), typ="disk", fstype=None):
    return {"name": name, "path": f"/dev/{name}", "type": typ, "size": size, "model": model + "    ", "serial": serial,
            "rota": True, "tran": "sata", "fstype": fstype, "label": None, "uuid": None, "mountpoints": list(mounts),
            "rm": False, "hotplug": False, "children": list(children)}


def part(name, fstype=None, label=None, uuid=None, mounts=(), children=(), typ="part"):
    return {"name": name, "path": f"/dev/{name}", "type": typ, "size": 1000, "fstype": fstype, "label": label, "uuid": uuid,
            "mountpoints": list(mounts), "children": list(children)}


def lsblk(tmp):
    root_lv = part("vg-root", "ext4", mounts=["/"], typ="lvm")
    swap_lv = part("vg-swap", "swap", mounts=["[SWAP]"], typ="lvm")
    crypt = part("luks-1", "LVM2_member", children=[root_lv, swap_lv], typ="crypt")
    return {"blockdevices": [
        disk("nvme0n1", [part("nvme0n1p1", "vfat", mounts=["/boot/efi"]), part("nvme0n1p2", "crypto_LUKS", children=[crypt])],
             serial="S5H9NS0N", model="Samsung SSD 980"),
        disk("sda", [part("sda1", "xfs", "disk1", "u-disk1", mounts=[f"{S.DISKS}/disk1"])], serial="WD-AAA"),
        disk("sdb", [part("sdb1", "xfs", "parity1", "u-parity1", mounts=[f"{S.PARITY}/parity1"])], serial="WD-PPP"),
        disk("sdc", [part("sdc1", "ntfs", "OLD", "u-ntfs")], serial="ZA1B2C3D"),
        disk("sdd", [part("sdd1", "iso9660", mounts=["/run/live/medium"])], serial="USB123", model="SanDisk"),
        disk("vdb", serial=None, model="", size=8 * 1024 ** 3),
        disk("loop0", typ="loop"), disk("zram0"),
    ]}


@pytest.fixture
def fake(env, monkeypatch):
    tmp, _ = env
    state = {"lsblk": lsblk(tmp)}
    answers = [((storage.LSBLK,), lambda argv: json.dumps(state["lsblk"])),
               ((storage.SMARTCTL,), json.dumps({"smart_status": {"passed": True}, "temperature": {"current": 34}}))]
    fake = FakeRunner(answers)
    fake.state = state
    monkeypatch.setattr(runner, "run", fake.run)
    return fake


def by_name(disks):
    return {d["name"]: d for d in disks}


# --- reading -----------------------------------------------------------------------------------
def test_roles(fake):
    result = storage.disk_list(Ctx(), {})
    disks = by_name(result["disks"])
    assert sorted(disks) == ["nvme0n1", "sda", "sdb", "sdc", "sdd", "vdb"]
    assert disks["nvme0n1"]["system"] and disks["nvme0n1"]["role"] == "system"
    assert set(disks["nvme0n1"]["mounts"]) == {"/boot/efi", "/", "[SWAP]"}
    assert (disks["sda"]["role"], disks["sda"]["label"], disks["sda"]["fstype"]) == ("data", "disk1", "xfs")
    assert (disks["sdb"]["role"], disks["sdb"]["label"]) == ("parity", "parity1")
    assert disks["sdc"]["role"] == "unassigned" and not disks["sdc"]["mounted"] and disks["sdc"]["fstype"] == "ntfs"
    assert disks["sdd"]["live"] and disks["sdd"]["role"] == "system"
    assert disks["vdb"]["serial"] == "" and disks["vdb"]["model"] == "" and disks["sda"]["model"] == "WDC WD40EFRX"
    assert disks["sda"]["smart"]["temperature"] == 34 and result["live"] is False


ATA = {"smart_status": {"passed": True}, "temperature": {"current": 41}, "power_on_time": {"hours": 21000},
       "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 8}}, {"id": 197, "raw": {"value": 2}}]}}
NVME = {"device": {"type": "nvme"}, "smart_status": {"passed": True}, "temperature": {"current": 55},
        "nvme_smart_health_information_log": {"media_errors": 0}}
ASLEEP = {"smartctl": {"messages": [{"string": "Device is in STANDBY mode, exit(2)"}], "exit_status": 2}}
NONE = {"smartctl": {"messages": [{"string": "Unable to detect device type"}], "exit_status": 1}}


def test_smart_summaries():
    ata = storage.smart_summary(ATA)
    assert (ata["passed"], ata["temperature"], ata["reallocated"], ata["pending"], ata["power_on_hours"], ata["nvme"]) == \
        (True, 41, 8, 2, 21000, False)
    nvme = storage.smart_summary(NVME)
    assert nvme["nvme"] is True and nvme["media_errors"] == 0 and nvme["error"] is None
    assert storage.smart_summary(ASLEEP)["asleep"] is True and storage.smart_summary(ASLEEP)["error"] is None
    assert storage.smart_summary(NONE)["error"] == "Unable to detect device type"


def test_smart_cache(fake):
    dev = {"name": "sda", "path": "/dev/sda", "serial": "WD-AAA"}
    fake.answers[1] = ((storage.SMARTCTL,), (0, json.dumps(ATA)))
    assert storage.smart(dev)["temperature"] == 41
    assert storage.smart(dev)["temperature"] == 41
    assert sum(1 for c in fake.calls if c[0] == storage.SMARTCTL) == 1                 # cached
    assert [c for c in fake.calls if c[0] == storage.SMARTCTL][0] == [storage.SMARTCTL, "--json", "--all",
                                                                      "--nocheck=standby", "/dev/sda"]
    fake.answers[1] = ((storage.SMARTCTL,), (2, json.dumps(ASLEEP)))
    asleep = storage.smart(dev, refresh=True)
    assert asleep["asleep"] is True and asleep["temperature"] == 41                    # the last real reading


# --- fstab ---------------------------------------------------------------------------------------
def test_fstab_block(env):
    tmp, _ = env
    original = "# /etc/fstab: static file system information.\nUUID=abc / ext4 errors=remount-ro 0 1\n"
    (tmp / "etc/fstab").write_text(original)
    entries = [{"kind": "disk", "role": "data", "label": "disk1", "uuid": "u-1", "fs": "xfs"},
               {"kind": "disk", "role": "parity", "label": "parity1", "uuid": "u-2", "fs": "ext4"},
               {"kind": "pool", "branches": ["disk1", "disk2"], "policy": "epmfs"},
               {"kind": "isos", "source": f"{S.POOL}/isos"}]
    assert fsutil.write_block(entries) is True
    text = (tmp / "etc/fstab").read_text()
    assert text.startswith(original + "\n" + fsutil.BEGIN + "\n") and text.endswith(fsutil.END + "\n")
    assert f"UUID=u-1 {S.DISKS}/disk1 xfs defaults,nofail,x-systemd.device-timeout=30s 0 0" in text
    assert f"UUID=u-2 {S.PARITY}/parity1 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2" in text
    assert (f"{S.DISKS}/disk1:{S.DISKS}/disk2 {S.POOL} fuse.mergerfs defaults,nofail,allow_other,cache.files=off,"
            "dropcacheonclose=true,category.create=epmfs,moveonenospc=true,fsname=nimbus-pool,"
            f"x-systemd.requires-mounts-for={S.DISKS}/disk1,x-systemd.requires-mounts-for={S.DISKS}/disk2 0 0") in text
    assert f"{S.POOL}/isos {S.ISOS} none bind,nofail,x-systemd.requires-mounts-for={S.POOL} 0 0" in text
    back = fsutil.read_block()
    assert back[0] == entries[0] and back[2] == {"kind": "pool", "branches": ["disk1", "disk2"], "policy": "epmfs"}
    assert back[3] == {"kind": "isos", "source": f"{S.POOL}/isos"} and len(os.listdir(tmp / "fstab-backups")) == 1
    assert (tmp / "fstab-backups" / os.listdir(tmp / "fstab-backups")[0]).read_text() == original
    assert fsutil.write_block(back) is False                                           # unchanged: no write, no backup
    text = (tmp / "etc/fstab").read_text().replace(fsutil.END, "tmpfs /scratch tmpfs defaults 0 0\n" + fsutil.END)
    (tmp / "etc/fstab").write_text(text + "# after the block\n")
    fsutil.write_block([e for e in fsutil.read_block() if e["kind"] != "isos"])
    text = (tmp / "etc/fstab").read_text()
    assert "tmpfs /scratch tmpfs defaults 0 0" in text and text.endswith("# after the block\n") and "bind" not in text


def test_fstab_keeps_ten_backups(env):
    tmp, _ = env
    (tmp / "etc/fstab").write_text("UUID=abc / ext4 defaults 0 1\n")
    for i in range(12):
        fsutil.write_block([{"kind": "disk", "role": "data", "label": f"d{i}", "uuid": f"u{i}", "fs": "xfs"}])
    assert len(os.listdir(tmp / "fstab-backups")) == 10


# --- format, mount, unmount ------------------------------------------------------------------------
GOOD = {"device": "/dev/sdc", "serial": "ZA1B2C3D", "fs": "xfs", "label": "disk2", "role": "data"}


@pytest.mark.parametrize("changes, field, text", [
    ({"device": "/dev/nvme0n1", "serial": "S5H9NS0N"}, "device", "running system"),
    ({"device": "/dev/sdd", "serial": "USB123"}, "device", "live medium"),
    ({"device": "/dev/sda", "serial": "WD-AAA"}, "device", "Unmount them first"),
    ({"device": "/dev/sdz"}, "device", "not a disk on this computer"),
    ({"serial": "ZA1B2C3"}, "serial", "not the serial number"),
    ({"device": "/dev/vdb", "serial": "ZA1B2C3D"}, "serial", "not the name of this disk"),
    ({"label": "disk1"}, "label", "already used"),
])
def test_format_refusals(fake, changes, field, text):
    fsutil.write_block([{"kind": "disk", "role": "data", "label": "disk1", "uuid": "u-disk1", "fs": "xfs"}])
    with pytest.raises(ValidationError) as err:
        storage.disk_format(Ctx(), {**GOOD, **changes})
    assert err.value.field == field and text in err.value.message
    assert not any(c[0] in (storage.WIPEFS, storage.PARTED) for c in fake.calls)


def test_format_refused_in_the_live_session(fake, monkeypatch):
    monkeypatch.setattr(storage, "live_session", lambda: True)
    with pytest.raises(ValidationError) as err:
        storage.disk_format(Ctx(), GOOD)
    assert "Install NimbusOS to set up storage" in err.value.message


def test_format_arguments_are_checked():
    verb = REGISTRY.get("disk.format")
    for bad in ({**GOOD, "device": "/dev/sdc; rm -rf /"}, {**GOOD, "fs": "ntfs"}, {**GOOD, "label": "Disk 1"},
                {**GOOD, "role": "cache"}, {**GOOD, "device": "../../dev/sda"}):
        with pytest.raises(ValidationError):
            REGISTRY.validate(verb, bad)


def test_format(fake, env):
    tmp, mounted = env
    new_part = tmp / "sdc1"
    new_part.write_text("")
    after = lsblk(tmp)
    after["blockdevices"][3]["children"] = [{**part("sdc1"), "path": str(new_part)}]
    calls = {"n": 0}

    def listing(argv):
        calls["n"] += 1
        return json.dumps(lsblk(tmp) if calls["n"] == 1 else after)
    fake.answers[0] = ((storage.LSBLK,), listing)
    fake.answers += [((storage.WIPEFS,), ""), ((storage.PARTED,), ""), ((storage.UDEVADM,), ""),
                     (("/usr/sbin/mkfs.xfs",), ""), ((storage.BLKID,), "0b7c2e7d-5c6a-4f3e-9d2a-1e2f3a4b5c6d\n"),
                     ((storage.MOUNT,), lambda argv: mounted.add(argv[1]) or "")]
    ctx = Ctx()
    result = storage.disk_format(ctx, GOOD)
    assert result == {"label": "disk2", "uuid": "0b7c2e7d-5c6a-4f3e-9d2a-1e2f3a4b5c6d", "mountpoint": f"{S.DISKS}/disk2",
                      "fs": "xfs", "role": "data"}
    commands = [c for c in fake.calls if c[0] not in (storage.LSBLK, storage.SMARTCTL)]
    assert commands == [
        [storage.WIPEFS, "--all", "--force", "/dev/sdc1"], [storage.WIPEFS, "--all", "--force", "/dev/sdc"],
        [storage.PARTED, "--script", "/dev/sdc", "mklabel", "gpt", "mkpart", "disk2", "xfs", "1MiB", "100%"],
        [storage.UDEVADM, "settle", "--timeout=30"],
        ["/usr/sbin/mkfs.xfs", "-f", "-L", "disk2", str(new_part)],
        [storage.BLKID, "-o", "value", "-s", "UUID", str(new_part)],
        [storage.MOUNT, f"{S.DISKS}/disk2"]]
    assert fsutil.read_block() == [{"kind": "disk", "role": "data", "label": "disk2",
                                    "uuid": "0b7c2e7d-5c6a-4f3e-9d2a-1e2f3a4b5c6d", "fs": "xfs"}]
    assert os.path.isdir(f"{S.DISKS}/disk2") and any("Erasing /dev/sdc" in line for line in ctx.lines)


def test_format_ext4_parity_options(fake, env):
    tmp, mounted = env
    new_part = tmp / "sdc1"
    new_part.write_text("")
    after = lsblk(tmp)
    after["blockdevices"][3]["children"] = [{**part("sdc1"), "path": str(new_part)}]
    seq = iter([lsblk(tmp), after, after])
    fake.answers[0] = ((storage.LSBLK,), lambda argv: json.dumps(next(seq)))
    fake.answers += [((storage.WIPEFS,), ""), ((storage.PARTED,), ""), ((storage.UDEVADM,), ""),
                     (("/usr/sbin/mkfs.ext4",), ""), ((storage.BLKID,), "abcdef12-3456\n"), ((storage.MOUNT,), "")]
    storage.disk_format(Ctx(), {**GOOD, "fs": "ext4", "role": "parity", "label": "parity2"})
    mkfs = next(c for c in fake.calls if c[0] == "/usr/sbin/mkfs.ext4")
    assert mkfs == ["/usr/sbin/mkfs.ext4", "-F", "-q", "-L", "parity2", "-m", "0", str(new_part)]
    assert fsutil.read_block()[0]["role"] == "parity"


def test_mount_an_existing_filesystem(fake, env):
    tmp, mounted = env
    fake.state["lsblk"]["blockdevices"][3]["children"][0].update(fstype="xfs", label="old", uuid="u-old")
    fake.answers.append(((storage.MOUNT,), lambda argv: mounted.add(argv[1]) or ""))
    result = storage.disk_mount(Ctx(), {"partition": "/dev/sdc1", "label": "old", "role": "data"})
    assert result == {"label": "old", "mountpoint": f"{S.DISKS}/old"}
    assert fsutil.read_block() == [{"kind": "disk", "role": "data", "label": "old", "uuid": "u-old", "fs": "xfs"}]
    with pytest.raises(ValidationError) as err:
        storage.disk_mount(Ctx(), {"partition": "/dev/nvme0n1p1", "label": "efi", "role": "data"})
    assert "in use already" in err.value.message


def test_mount_needs_a_supported_filesystem(fake):
    with pytest.raises(ValidationError) as err:
        storage.disk_mount(Ctx(), {"partition": "/dev/sdc1", "label": "old", "role": "data"})
    assert "no XFS, ext4 or btrfs" in err.value.message


def test_unmount(fake, env):
    tmp, mounted = env
    fsutil.write_block([{"kind": "disk", "role": "data", "label": "disk1", "uuid": "u-1", "fs": "xfs"},
                        {"kind": "disk", "role": "data", "label": "disk2", "uuid": "u-2", "fs": "xfs"},
                        {"kind": "pool", "branches": ["disk1"], "policy": "mfs"}])
    mounted.add(f"{S.DISKS}/disk2")
    fake.answers.append(((storage.UMOUNT,), lambda argv: mounted.discard(argv[1]) or ""))
    with pytest.raises(ValidationError) as err:
        storage.disk_unmount(Ctx(), {"label": "disk1"})
    assert "part of the pool" in err.value.message
    storage.disk_unmount(Ctx(), {"label": "disk2"})
    assert [e.get("label") for e in fsutil.read_block()] == ["disk1", None]
    assert [storage.UMOUNT, f"{S.DISKS}/disk2"] in fake.calls


# --- the pool --------------------------------------------------------------------------------------
@pytest.fixture
def disks_ready(fake, env):
    tmp, mounted = env
    fsutil.write_block([{"kind": "disk", "role": "data", "label": "disk1", "uuid": "u-1", "fs": "xfs"},
                        {"kind": "disk", "role": "data", "label": "disk2", "uuid": "u-2", "fs": "xfs"},
                        {"kind": "disk", "role": "parity", "label": "parity1", "uuid": "u-3", "fs": "xfs"}])
    mounted.update({f"{S.DISKS}/disk1", f"{S.DISKS}/disk2", f"{S.PARITY}/parity1"})
    fake.answers += [((storage.MOUNT,), lambda argv: mounted.add(argv[1]) or ""),
                     ((storage.UMOUNT,), lambda argv: mounted.discard(argv[1]) or "")]
    return mounted


def test_pooled(fake, disks_ready, monkeypatch):
    result = storage.pool_apply(Ctx(), {"mode": "pooled", "policy": "epmfs", "data": ["disk1", "disk2"], "parity": []})
    assert result["mode"] == "pooled" and [storage.MOUNT, S.POOL] in fake.calls
    assert [e for e in fsutil.read_block() if e["kind"] == "pool"] == [{"kind": "pool", "branches": ["disk1", "disk2"],
                                                                         "policy": "epmfs"}]
    os.makedirs(S.POOL, exist_ok=True)
    open(os.path.join(S.POOL, ".mergerfs"), "w").close()
    attrs = {}
    monkeypatch.setattr(os, "setxattr", lambda path, name, value: attrs.update({name: value}))
    mounts_before = sum(1 for c in fake.calls if c[0] == storage.MOUNT)
    storage.pool_apply(Ctx(), {"mode": "pooled", "policy": "mfs", "data": ["disk1"], "parity": []})
    assert attrs == {"user.mergerfs.branches": f"{S.DISKS}/disk1".encode(), "user.mergerfs.category.create": b"mfs"}
    assert sum(1 for c in fake.calls if c[0] == storage.MOUNT) == mounts_before        # changed live, not remounted


@pytest.mark.parametrize("args, field, text", [
    ({"mode": "pooled", "data": []}, "data", "at least one data disk"),
    ({"mode": "pooled", "data": ["disk9"]}, "data", "No disk is set up"),
    ({"mode": "pooled", "data": ["parity1"]}, "data", "not a mounted data disk"),
    ({"mode": "pooled", "data": ["disk1"], "parity": ["parity1"]}, "parity", "only used in Pooled + parity"),
    ({"mode": "parity", "data": ["disk1"], "parity": []}, "parity", "one or two parity disks"),
])
def test_pool_refusals(fake, disks_ready, monkeypatch, args, field, text):
    monkeypatch.setattr(storage, "SNAPRAID", "/bin/sh")                              # "installed"
    with pytest.raises(ValidationError) as err:
        storage.pool_apply(Ctx(), {"policy": "mfs", "parity": [], **args})
    assert err.value.field == field and text in err.value.message


def test_parity(fake, disks_ready, monkeypatch, env):
    tmp, _ = env
    monkeypatch.setattr(storage, "SNAPRAID", "/nonexistent/snapraid")
    with pytest.raises(ValidationError) as err:
        storage.pool_apply(Ctx(), {"mode": "parity", "policy": "mfs", "data": ["disk1"], "parity": ["parity1"]})
    assert "SnapRAID isn't installed" in err.value.message
    monkeypatch.setattr(storage, "SNAPRAID", "/bin/sh")
    sizes = {f"{S.DISKS}/disk1": 4000, f"{S.DISKS}/disk2": 8000, f"{S.PARITY}/parity1": 6000}
    monkeypatch.setattr(storage, "_fs_size", lambda p: sizes[p])
    with pytest.raises(ValidationError) as err:
        storage.pool_apply(Ctx(), {"mode": "parity", "policy": "mfs", "data": ["disk1", "disk2"], "parity": ["parity1"]})
    assert "smaller than the largest data disk" in err.value.message
    sizes[f"{S.PARITY}/parity1"] = 8000
    storage.pool_apply(Ctx(), {"mode": "parity", "policy": "mfs", "data": ["disk1", "disk2"], "parity": ["parity1"]})
    conf = (tmp / "etc/snapraid.conf").read_text().splitlines()
    assert conf[0].startswith("# Generated by nimbus-control")
    assert f"parity {S.PARITY}/parity1/snapraid.parity" in conf
    assert f"content {tmp}/privd/snapraid.content" in conf and f"content {S.DISKS}/disk2/.snapraid.content" in conf
    assert f"data disk1 {S.DISKS}/disk1/" in conf and "exclude .recycle/" in conf
    storage.pool_apply(Ctx(), {"mode": "pooled", "policy": "mfs", "data": ["disk1", "disk2"], "parity": []})
    assert not (tmp / "etc/snapraid.conf").exists()
    assert any(n.startswith("snapraid.conf.") for n in os.listdir(tmp / "privd"))       # kept, not deleted


def test_two_parity_disks_render():
    conf = storage.render_snapraid(["disk1"], ["parity1", "parity2"])
    assert f"parity {S.PARITY}/parity1/snapraid.parity" in conf and f"2-parity {S.PARITY}/parity2/snapraid.2-parity" in conf


def test_single_mode_and_a_busy_pool(fake, disks_ready):
    storage.pool_apply(Ctx(), {"mode": "pooled", "policy": "mfs", "data": ["disk1"], "parity": []})
    fake.answers.insert(0, ((storage.UMOUNT, S.POOL), (32, "target is busy")))
    with pytest.raises(ValidationError) as err:
        storage.pool_apply(Ctx(), {"mode": "single", "policy": "mfs", "data": [], "parity": []})
    assert "in use" in err.value.message and any(e["kind"] == "pool" for e in fsutil.read_block())
    fake.answers.pop(0)
    storage.pool_apply(Ctx(), {"mode": "single", "policy": "mfs", "data": [], "parity": []})
    assert not any(e["kind"] == "pool" for e in fsutil.read_block())


def test_pool_refused_in_the_live_session(fake, monkeypatch):
    monkeypatch.setattr(storage, "live_session", lambda: True)
    with pytest.raises(ValidationError):
        storage.pool_apply(Ctx(), {"mode": "single", "policy": "mfs", "data": [], "parity": []})


# --- parity status ----------------------------------------------------------------------------------
DIFF = """Loading state from /var/lib/nimbus-privd/snapraid.content...
Comparing...
add disk1/films/new.mkv
   41522 equal
       3 added
       1 removed
       2 updated
       0 moved
       0 copied
       0 restored
There are differences!
"""


def test_parse_diff():
    assert storage.parse_diff(DIFF) == {"equal": 41522, "added": 3, "removed": 1, "updated": 2, "moved": 0,
                                        "copied": 0, "restored": 0}


def test_snapraid_status(fake, env, monkeypatch):
    tmp, _ = env
    monkeypatch.setattr(storage, "SNAPRAID", "/bin/sh")
    (tmp / "etc/snapraid.conf").write_text("# conf\n")
    (tmp / "cache").mkdir()
    (tmp / "cache/snapraid-status.json").write_text(json.dumps({"last_sync": "2026-11-18T03:31:00+00:00", "last_sync_ok": True}))
    fake.answers.append((("/bin/sh", "-c"), (2, DIFF)))
    status = storage.snapraid_status(Ctx(), {})
    assert status["configured"] and status["last"]["last_sync_ok"] is True and status["running"] is False
    assert status["diff"]["unprotected"] == 6 and status["diff"]["exit"] == 2
    storage.snapraid_status(Ctx(), {})
    assert sum(1 for c in fake.calls if c[0] == "/bin/sh") == 1                         # cached for 10 minutes
