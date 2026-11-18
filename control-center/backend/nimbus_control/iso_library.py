# =============================================================================
# nimbus_control/iso_library.py: the ISO library (/var/lib/nimbus-vms/isos)
# =============================================================================
# What: IsoLibrary lists, receives and deletes installer images. When an
#       "isos" share exists, privd bind-mounts it onto the same folder, so
#       files copied in over SMB appear here too (NOTE-27).
#         list()       regular *.iso and *.img files (never links, never
#                      unfinished uploads)
#         path(name)   the full path of an existing ISO, for a VM's CD-ROM
#         uploads      chunked and streamed: begin(name, size) checks the name,
#                      the size limit and the free space and makes a hidden
#                      ".upload-<id>.part" file; write(id, offset, chunks)
#                      appends one chunk (each at most 16 MiB, in order);
#                      finish(id) checks the size and gives the file its
#                      name without ever replacing an existing file;
#                      cancel(id) removes it. Unfinished uploads are removed
#                      after an hour without data, and at startup.
#       Every file is opened relative to a handle on the library folder with
#       O_NOFOLLOW: a name is a plain file name (validate.iso_name), and a
#       link planted in the folder (it is writable over SMB) is never
#       followed. ISOs are made 0644 so QEMU (libvirt-qemu) can read them.
# Why:  spec Tab B ("Chunked, streamed upload with a size limit, plus list
#       and delete") and spec Security ("Reject path traversal in ISO
#       uploads"); docs/PLAN.md §4.3.
# =============================================================================

import asyncio
import errno
import os
import secrets
import stat
import time

from nimbus_common import validate as V

from .errors import ApiError

CHUNK_MAX = 16 * 1024 * 1024
FREE_MARGIN = 256 * 1024 * 1024
IDLE_SECONDS = 3600
MAX_UPLOADS = 4
PART_PREFIX = ".upload-"
EXTENSIONS = (".iso", ".img")


class Upload:
    def __init__(self, name, size, part, ident):
        self.name = name
        self.size = size
        self.part = part
        self.ident = ident               # (st_dev, st_ino) of the part file
        self.received = 0
        self.touched = time.monotonic()
        self.lock = asyncio.Lock()


class IsoLibrary:
    def __init__(self, root, max_bytes):
        self.root = root
        self.max_bytes = max_bytes
        self.uploads = {}

    # --- the folder ----------------------------------------------------------------
    def _open_root(self):
        try:
            return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise ApiError(503, f"The ISO folder {self.root} can't be opened: {exc.strerror}.") from None

    def free_bytes(self):
        try:
            st = os.statvfs(self.root)
            return st.f_bavail * st.f_frsize
        except OSError:
            return None

    def list(self):
        fd = self._open_root()
        try:
            out = []
            with os.scandir(fd) as entries:
                for e in entries:
                    if e.name.startswith(".") or not e.name.lower().endswith(EXTENSIONS):
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                    out.append({"name": e.name, "size": st.st_size, "modified": int(st.st_mtime)})
            return sorted(out, key=lambda x: x["name"].lower())
        finally:
            os.close(fd)

    def _lstat(self, fd, name):
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return st

    def path(self, name, field="iso"):
        """The full path of the library ISO NAME, for a VM."""
        try:
            V.iso_name(name, field)
        except V.ValidationError as exc:
            raise ApiError(422, exc.message, exc.field) from None
        fd = self._open_root()
        try:
            st = self._lstat(fd, name)
        finally:
            os.close(fd)
        if st is None or not stat.S_ISREG(st.st_mode):
            raise ApiError(422, f"There is no ISO named '{name}' in the library.", field)
        return os.path.join(self.root, name)

    def delete(self, name):
        try:
            V.iso_name(name)
        except V.ValidationError as exc:
            raise ApiError(422, exc.message, exc.field) from None
        fd = self._open_root()
        try:
            st = self._lstat(fd, name)
            if st is None or not stat.S_ISREG(st.st_mode):
                raise ApiError(404, f"There is no ISO named '{name}' in the library.")
            os.unlink(name, dir_fd=fd)
        finally:
            os.close(fd)

    # --- uploads -----------------------------------------------------------------------
    def cleanup(self, everything=False):
        """Remove unfinished uploads: all of them (at startup), or those idle
        for an hour."""
        now = time.monotonic()
        for uid, up in list(self.uploads.items()):
            if now - up.touched > IDLE_SECONDS:
                self._drop(uid)
        if everything:
            try:
                fd = self._open_root()
            except ApiError:
                return
            try:
                for name in os.listdir(fd):
                    if name.startswith(PART_PREFIX) and name.endswith(".part"):
                        try:
                            os.unlink(name, dir_fd=fd)
                        except OSError:
                            pass
            finally:
                os.close(fd)

    def _drop(self, uid):
        up = self.uploads.pop(uid, None)
        if up is None:
            return
        fd = self._open_root()
        try:
            st = self._lstat(fd, up.part)
            if st is not None and (st.st_dev, st.st_ino) == up.ident:
                os.unlink(up.part, dir_fd=fd)
        finally:
            os.close(fd)

    def begin(self, name, size):
        self.cleanup()
        try:
            V.iso_name(name)
        except V.ValidationError as exc:
            raise ApiError(422, exc.message, exc.field) from None
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ApiError(422, "The file is empty.", "size")
        if size > self.max_bytes:
            raise ApiError(413, f"The file is larger than the limit of {self.max_bytes / 1024 ** 3:.0f} GiB.", "size")
        if len(self.uploads) >= MAX_UPLOADS:
            raise ApiError(409, f"{MAX_UPLOADS} uploads are already running. Wait until one has finished.")
        if any(up.name == name for up in self.uploads.values()):
            raise ApiError(409, f"'{name}' is already being uploaded.", "name")
        free = self.free_bytes()
        if free is not None and free < size + FREE_MARGIN:
            raise ApiError(507, f"There isn't enough free space in {self.root} for this file "
                                f"({size / 1024 ** 3:.1f} GiB needed, {free / 1024 ** 3:.1f} GiB free).", "size")
        fd = self._open_root()
        try:
            if self._lstat(fd, name) is not None:
                raise ApiError(409, f"An ISO named '{name}' is already in the library. Delete it first, "
                                    "or rename the file.", "name")
            uid = secrets.token_hex(8)
            part = f"{PART_PREFIX}{uid}.part"
            pfd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
            try:
                os.fchmod(pfd, 0o644)                    # the service's umask would make it 0640
                st = os.fstat(pfd)
            finally:
                os.close(pfd)
        finally:
            os.close(fd)
        self.uploads[uid] = Upload(name, size, part, (st.st_dev, st.st_ino))
        return {"id": uid, "chunk_bytes": CHUNK_MAX // 2, "received": 0}

    def _upload(self, uid):
        up = self.uploads.get(uid) if isinstance(uid, str) else None
        if up is None:
            raise ApiError(404, "This upload is unknown or was cancelled. Start it again.")
        return up

    async def write(self, uid, offset, chunks):
        up = self._upload(uid)
        async with up.lock:
            if offset != up.received:
                raise ApiError(409, f"The upload expected the data from byte {up.received}.", "offset")
            fd = self._open_root()
            try:
                pfd = os.open(up.part, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError:
                os.close(fd)
                self._drop(uid)
                raise ApiError(409, "The unfinished upload file is gone. Start the upload again.") from None
            os.close(fd)
            try:
                st = os.fstat(pfd)
                if (st.st_dev, st.st_ino) != up.ident or not stat.S_ISREG(st.st_mode):
                    raise ApiError(409, "The unfinished upload file was replaced. Start the upload again.")
                written, buffer = 0, bytearray()
                async for piece in chunks:
                    if written + len(buffer) + len(piece) > CHUNK_MAX or offset + written + len(buffer) + len(piece) > up.size:
                        raise ApiError(413, "This chunk is too large for the upload.", "offset")
                    buffer += piece
                    if len(buffer) >= 1024 * 1024:
                        await asyncio.to_thread(_pwrite_all, pfd, bytes(buffer), offset + written)
                        written += len(buffer)
                        buffer = bytearray()
                if buffer:
                    await asyncio.to_thread(_pwrite_all, pfd, bytes(buffer), offset + written)
                    written += len(buffer)
            except OSError as exc:
                raise ApiError(507 if exc.errno == errno.ENOSPC else 500,
                               f"Writing the ISO failed: {exc.strerror}.") from None
            finally:
                os.close(pfd)
            up.received += written
            up.touched = time.monotonic()
            return {"received": up.received, "size": up.size}

    def finish(self, uid):
        up = self._upload(uid)
        if up.lock.locked():
            raise ApiError(409, "A chunk is still being written.")
        if up.received != up.size:
            raise ApiError(409, f"The upload is incomplete ({up.received} of {up.size} bytes).")
        fd = self._open_root()
        try:
            st = self._lstat(fd, up.part)
            if st is None or (st.st_dev, st.st_ino) != up.ident:
                self.uploads.pop(uid, None)
                raise ApiError(409, "The unfinished upload file was replaced. Start the upload again.")
            try:
                # A hard link never replaces an existing file (rename would).
                os.link(up.part, up.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.unlink(up.part, dir_fd=fd)
            except FileExistsError:
                raise ApiError(409, f"An ISO named '{up.name}' appeared in the library meanwhile.", "name") from None
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EMLINK):
                    raise
                if self._lstat(fd, up.name) is not None:          # a file system without hard links
                    raise ApiError(409, f"An ISO named '{up.name}' appeared in the library meanwhile.", "name") from None
                os.rename(up.part, up.name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
        self.uploads.pop(uid, None)
        return {"name": up.name, "size": up.size}

    def cancel(self, uid):
        self._upload(uid)
        self._drop(uid)


def _pwrite_all(fd, data, offset):
    view = memoryview(data)
    while view:
        n = os.pwrite(fd, view, offset)
        view, offset = view[n:], offset + n
