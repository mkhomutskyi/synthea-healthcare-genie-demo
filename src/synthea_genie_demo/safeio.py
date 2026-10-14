"""Local file primitives that never follow a symlink at the final component.

- ``read_regular``: read a regular file once, through a no-follow handle, with a size cap.
- ``write_private_atomic``: replace one file atomically (private temp file, fsync, rename,
  directory fsync), refusing a final component that is a symlink or not a regular file.
- ``open_real_directory``: a directory handle reached one no-follow component at a time.
- ``rename_noreplace``: rename that fails if the destination exists, using the kernel's
  no-replace rename on Darwin and Linux when available.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .jsonio import sha256_bytes

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_RENAME_EXCL_DARWIN = 0x00000004
_RENAME_NOREPLACE_LINUX = 1


class SafeFileError(OSError):
    pass


@dataclass(frozen=True)
class FileIdentity:
    """A regular file's identity and exact content, as read through a no-follow handle."""

    device: int
    inode: int
    size: int
    sha256: str


def _check_name(name: str) -> None:
    if not name or os.path.basename(name) != name or name in (os.curdir, os.pardir):
        raise SafeFileError(f"invalid file name {name!r}")


def _read_fd(fd: int, limit: int, shown: str) -> tuple[bytes, os.stat_result]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SafeFileError(f"{shown} is not a regular file")
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(fd, min(1 << 20, limit + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise SafeFileError(f"{shown} is larger than {limit} bytes")
    return b"".join(chunks), info


def read_regular(path: Path, limit: int) -> bytes:
    """The bytes of a regular file, read once; a symlink, FIFO or directory is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SafeFileError(f"{path} is a symlink; refusing to read it") from exc
        raise
    try:
        data, _ = _read_fd(fd, limit, str(path))
    finally:
        os.close(fd)
    return data


def file_identity(path: Path, limit: int) -> FileIdentity:
    """Device, inode, size and SHA-256 of a regular file, read through one no-follow handle."""
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SafeFileError(f"{path} is a symlink") from exc
        raise
    try:
        data, info = _read_fd(fd, limit, str(path))
    finally:
        os.close(fd)
    return FileIdentity(info.st_dev, info.st_ino, len(data), sha256_bytes(data))


def write_private_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace ``path`` with ``data``.

    The bytes go to a fresh temporary file (O_CREAT | O_EXCL | O_NOFOLLOW, private) in the
    same directory, opened through a no-follow directory handle; the file is fsynced, given
    ``mode``, renamed over ``path`` and the directory is fsynced. An existing ``path`` that
    is a symlink or not a regular file is refused. A crash leaves either the old file or
    the new one, never a partial file; a failed write removes its temporary file.
    """
    name = path.name
    _check_name(name)
    dir_fd = os.open(path.parent, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC)
    temp: str | None = None
    try:
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise SafeFileError(f"{path.parent} is not a directory")
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise SafeFileError(f"{path} is a symlink or not a regular file; refusing to write")
        temp = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC, 0o600, dir_fd=dir_fd
        )
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp = None
        os.fsync(dir_fd)
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=dir_fd)
        os.close(dir_fd)


def open_real_directory(path: Path) -> int:
    """A handle on ``path``, opened from ``/`` one no-follow component at a time.

    ``path`` must be absolute and contain no symlink; a component swapped for a symlink
    while it is being opened is refused by O_NOFOLLOW rather than followed.
    """
    absolute = os.path.abspath(path)
    if os.path.realpath(absolute) != absolute:
        raise SafeFileError(f"{path} passes through a symlink; use a real directory")
    fd = os.open("/", os.O_RDONLY | DIRECTORY | CLOEXEC)
    try:
        for part in Path(absolute).parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise SafeFileError(f"{path} is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _libc() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("c")
    try:
        return ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None


def _kernel_noreplace(src: str, dst: str, dir_fd: int) -> bool:
    """True if the kernel performed the no-replace rename; False if it is unavailable."""
    libc = _libc()
    if libc is None:
        return False
    if sys.platform == "darwin":
        function, flag = getattr(libc, "renameatx_np", None), _RENAME_EXCL_DARWIN
    elif sys.platform.startswith("linux"):
        function, flag = getattr(libc, "renameat2", None), _RENAME_NOREPLACE_LINUX
    else:
        return False
    if function is None:
        return False
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
    function.argtypes += [ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(dir_fd, os.fsencode(src), dir_fd, os.fsencode(dst), flag) == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
        return False
    raise OSError(code, os.strerror(code), dst)


def rename_noreplace(src: str, dst: str, dir_fd: int) -> None:
    """Rename ``src`` to ``dst`` inside ``dir_fd``; FileExistsError if ``dst`` exists.

    Uses renameatx_np(RENAME_EXCL) on Darwin and renameat2(RENAME_NOREPLACE) on Linux.
    Where neither is available, falls back to a no-follow existence check then rename,
    which a directory rename still refuses for a non-empty existing directory.
    """
    _check_name(src)
    _check_name(dst)
    if _kernel_noreplace(src, dst, dir_fd):
        return
    try:
        os.stat(dst, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
