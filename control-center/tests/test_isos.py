# =============================================================================
# control-center/tests/test_isos.py: the ISO library and its uploads
# =============================================================================
# What: IsoLibrary on a temporary folder:
#         - a chunked upload arrives byte for byte, 0644, with no part file
#           left; list() shows only regular .iso/.img files;
#         - names that leave the folder or hide (../, /, a leading dot, NUL,
#           other extensions) are refused before any file is made;
#         - size: empty, over the limit, more than the free space;
#         - order: a chunk at the wrong offset, a chunk past the size, an
#           early finish; finishing never replaces a file that appeared;
#         - links: a link planted in the folder is not listed, not used,
#           not deleted through; a part file swapped for a link stops the
#           upload; the folder itself as a link is refused;
#         - cancel and the cleanups (idle uploads, everything at startup).
# Why:  spec Tab B and spec Security ("Reject path traversal in ISO
#       uploads"); docs/PLAN.md §4.3 and NOTE-67.
# =============================================================================

import asyncio
import os
import stat

import pytest

from nimbus_control import iso_library
from nimbus_control.errors import ApiError
from nimbus_control.iso_library import IsoLibrary

MB = 1024 * 1024


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "isos"
    root.mkdir()
    return IsoLibrary(str(root), max_bytes=64 * MB)


async def chunks(data, piece=64 * 1024):
    for i in range(0, len(data), piece):
        yield data[i:i + piece]


def write(lib, uid, offset, data):
    return asyncio.run(lib.write(uid, offset, chunks(data)))


def upload(lib, name, data, chunk=3 * MB):
    uid = lib.begin(name, len(data))["id"]
    for offset in range(0, len(data), chunk):
        write(lib, uid, offset, data[offset:offset + chunk])
    return lib.finish(uid)


def status(fn, *args):
    with pytest.raises(ApiError) as err:
        fn(*args)
    return err.value.status, err.value.field, err.value.message


def test_chunked_upload(lib):
    data = os.urandom(7 * MB + 123)
    assert upload(lib, "debian 13 (netinst).iso", data) == {"name": "debian 13 (netinst).iso", "size": len(data)}
    path = os.path.join(lib.root, "debian 13 (netinst).iso")
    assert open(path, "rb").read() == data
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert sorted(os.listdir(lib.root)) == ["debian 13 (netinst).iso"] and lib.uploads == {}
    assert lib.path("debian 13 (netinst).iso") == path


def test_list_shows_only_regular_iso_files(lib, tmp_path):
    for name in ("b.ISO", "a.img", "notes.txt", ".hidden.iso"):
        (tmp_path / "isos" / name).write_bytes(b"x")
    (tmp_path / "isos" / "folder.iso").mkdir()
    (tmp_path / "outside.iso").write_bytes(b"secret")
    os.symlink(tmp_path / "outside.iso", tmp_path / "isos" / "link.iso")
    assert [i["name"] for i in lib.list()] == ["a.img", "b.ISO"]
    assert lib.list()[0]["size"] == 1


@pytest.mark.parametrize("name", ["../escape.iso", "sub/dir.iso", "/etc/x.iso", ".hidden.iso", "-x.iso", "a\x00.iso",
                                  "notes.txt", "x.iso.part", "", None, 5])
def test_bad_names_are_refused(lib, name):
    code, field, _ = status(lib.begin, name, 100)
    assert code == 422 and field == "name"
    assert os.listdir(lib.root) == []


def test_sizes(lib, monkeypatch):
    assert status(lib.begin, "a.iso", 0)[0] == 422
    assert status(lib.begin, "a.iso", True)[0] == 422
    code, field, message = status(lib.begin, "a.iso", 65 * MB)
    assert (code, field) == (413, "size") and "limit of" in message
    monkeypatch.setattr(lib, "free_bytes", lambda: 100 * MB)
    code, _, message = status(lib.begin, "a.iso", 60 * MB)
    assert code == 507 and "isn't enough free space" in message
    assert os.listdir(lib.root) == []


def test_order_and_size_are_enforced(lib):
    uid = lib.begin("a.iso", 4 * MB)["id"]
    code, field, message = status(write, lib, uid, 1 * MB, b"x" * 10)
    assert (code, field) == (409, "offset") and "from byte 0" in message
    assert status(write, lib, uid, 0, b"x" * (5 * MB))[0] == 413
    write(lib, uid, 0, b"x" * MB)
    assert status(lib.finish, uid)[0] == 409                    # incomplete
    assert status(write, lib, uid, MB, b"x" * (iso_library.CHUNK_MAX + 1))[0] == 413
    write(lib, uid, MB, b"y" * (3 * MB))
    assert lib.finish(uid)["size"] == 4 * MB
    assert status(write, lib, uid, 0, b"x")[0] == 404           # finished uploads are gone


def test_existing_names_are_never_replaced(lib, tmp_path):
    (tmp_path / "isos" / "a.iso").write_bytes(b"original")
    code, field, _ = status(lib.begin, "a.iso", 10)
    assert (code, field) == (409, "name")
    uid = lib.begin("b.iso", 3)["id"]
    write(lib, uid, 0, b"new")
    (tmp_path / "isos" / "b.iso").write_bytes(b"appeared")       # copied in over SMB meanwhile
    assert status(lib.finish, uid)[0] == 409
    assert (tmp_path / "isos" / "b.iso").read_bytes() == b"appeared"
    assert status(lib.begin, "b.iso", 3)[0] == 409


def test_links_are_never_followed(lib, tmp_path):
    target = tmp_path / "precious.iso"
    target.write_bytes(b"keep me")
    os.symlink(target, tmp_path / "isos" / "evil.iso")
    assert status(lib.path, "evil.iso")[0] == 422
    assert status(lib.delete, "evil.iso")[0] == 404
    assert status(lib.begin, "evil.iso", 10)[0] == 409
    assert target.read_bytes() == b"keep me" and os.path.islink(tmp_path / "isos" / "evil.iso")


def test_a_swapped_part_file_stops_the_upload(lib, tmp_path):
    uid = lib.begin("a.iso", 10)["id"]
    part = tmp_path / "isos" / lib.uploads[uid].part
    part.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"")
    os.symlink(target, part)
    assert status(write, lib, uid, 0, b"x" * 10)[0] == 409
    assert target.read_bytes() == b""


def test_the_folder_itself_as_a_link_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "isos")
    lib = IsoLibrary(str(tmp_path / "isos"), 64 * MB)
    assert status(lib.list)[0] == 503
    assert status(lib.begin, "a.iso", 10)[0] == 503
    assert os.listdir(real) == []


def test_delete(lib, tmp_path):
    (tmp_path / "isos" / "a.iso").write_bytes(b"x")
    lib.delete("a.iso")
    assert os.listdir(lib.root) == []
    assert status(lib.delete, "a.iso")[0] == 404
    assert status(lib.delete, "../a.iso")[0] == 422


def test_cancel_and_cleanups(lib, tmp_path, monkeypatch):
    uid = lib.begin("a.iso", 10)["id"]
    lib.cancel(uid)
    assert os.listdir(lib.root) == [] and status(lib.cancel, uid)[0] == 404
    idle = lib.begin("b.iso", 10)["id"]
    lib.uploads[idle].touched -= iso_library.IDLE_SECONDS + 1
    lib.begin("c.iso", 10)                                       # begin() drops idle uploads
    assert idle not in lib.uploads and len(os.listdir(lib.root)) == 1
    (tmp_path / "isos" / "keep.iso").write_bytes(b"x")
    fresh = IsoLibrary(lib.root, lib.max_bytes)                  # a restart
    fresh.cleanup(everything=True)
    assert os.listdir(lib.root) == ["keep.iso"]


def test_upload_limits(lib):
    lib.begin("a.iso", 10)
    assert status(lib.begin, "a.iso", 10)[:2] == (409, "name")   # already being uploaded
    for name in ("b.iso", "c.iso", "d.iso"):
        lib.begin(name, 10)
    assert status(lib.begin, "e.iso", 10)[0] == 409
