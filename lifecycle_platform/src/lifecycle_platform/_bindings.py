"""Descriptor-relative path binding shared by provisioning and durable CAS."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True, slots=True)
class Identity:
    """An inode snapshot; `object_key` stays meaningful while its fd is retained."""

    device_major: int
    device_minor: int
    inode: int
    mode: int
    uid: int
    gid: int
    link_count: int
    size_bytes: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> Identity:
        return cls(
            device_major=os.major(value.st_dev),
            device_minor=os.minor(value.st_dev),
            inode=value.st_ino,
            mode=value.st_mode,
            uid=value.st_uid,
            gid=value.st_gid,
            link_count=value.st_nlink,
            size_bytes=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )

    @classmethod
    def from_fd(cls, fd: int) -> Identity:
        return cls.from_stat(os.fstat(fd))

    @property
    def object_key(self) -> tuple[int, int, int, int]:
        return self.device_major, self.device_minor, self.inode, stat.S_IFMT(self.mode)

    def same_object(self, other: Identity) -> bool:
        return self.object_key == other.object_key


@dataclass(frozen=True, slots=True)
class DirectoryLink:
    parent_fd: int
    child_fd: int
    name: str
    expected: Identity


class BoundDirectory:
    """A retained directory chain whose attachment is checked without replaying a path."""

    def __init__(self, anchor_fd: int, anchor: Identity, links: tuple[DirectoryLink, ...]) -> None:
        self._anchor_fd = anchor_fd
        self.anchor = anchor
        self.links = links
        self._closed = False

    @property
    def fd(self) -> int:
        return self.links[-1].child_fd if self.links else self._anchor_fd

    @property
    def identities(self) -> tuple[Identity, ...]:
        return (self.anchor, *(link.expected for link in self.links))

    def validate(self, *, exact_metadata: bool = False) -> bool:
        if self._closed:
            return False
        try:
            current_anchor = Identity.from_fd(self._anchor_fd)
            if not _identity_matches(current_anchor, self.anchor, exact_metadata):
                return False
            for link in self.links:
                child_now = Identity.from_fd(link.child_fd)
                entry_now = Identity.from_stat(
                    os.stat(link.name, dir_fd=link.parent_fd, follow_symlinks=False)
                )
                if not _identity_matches(child_now, link.expected, exact_metadata):
                    return False
                if not entry_now.same_object(child_now):
                    return False
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self._closed:
            return
        for link in reversed(self.links):
            os.close(link.child_fd)
        os.close(self._anchor_fd)
        self._closed = True

    def __enter__(self) -> BoundDirectory:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def bind_directory(path: Path, *, require_absolute: bool = False) -> BoundDirectory:
    """Open every component without symlink traversal and retain the full chain."""
    if require_absolute and not path.is_absolute():
        raise ValueError("the directory path must be absolute")
    anchor_path = Path("/") if path.is_absolute() else Path(".")
    components = _clean_components(path)
    anchor_fd = os.open(anchor_path, _DIRECTORY_FLAGS)
    anchor = Identity.from_fd(anchor_fd)
    links: list[DirectoryLink] = []
    parent_fd = anchor_fd
    try:
        for component in components:
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            expected = Identity.from_fd(child_fd)
            links.append(DirectoryLink(parent_fd, child_fd, component, expected))
            parent_fd = child_fd
    except BaseException:
        for link in reversed(links):
            os.close(link.child_fd)
        os.close(anchor_fd)
        raise
    return BoundDirectory(anchor_fd, anchor, tuple(links))


def open_regular_beneath(directory_fd: int, relative: PurePath, flags: int = _FILE_FLAGS) -> int:
    """Open a regular file by retained dirfds, rejecting symlinks and traversal."""
    components = _clean_relative_components(relative)
    parent_fd = os.dup(directory_fd)
    try:
        for component in components[:-1]:
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        file_fd = os.open(components[-1], flags | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    value = os.fstat(file_fd)
    if not stat.S_ISREG(value.st_mode):
        os.close(file_fd)
        raise ValueError("bound object is not a regular file")
    return file_fd


def read_all(fd: int, size_limit_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Read a retained regular file from offset zero with a fixed upper bound."""
    size_bytes = os.fstat(fd).st_size
    if size_bytes < 0 or size_bytes > size_limit_bytes:
        raise ValueError("file exceeds the supported size")
    chunks: list[bytes] = []
    offset_bytes = 0
    while offset_bytes < size_bytes:
        chunk = os.pread(fd, min(1024 * 1024, size_bytes - offset_bytes), offset_bytes)
        if not chunk:
            raise OSError("file became shorter while it was read")
        chunks.append(chunk)
        offset_bytes += len(chunk)
    return b"".join(chunks)


def digest_fd(fd: int) -> bytes:
    """Hash a retained fd while requiring an unchanged metadata snapshot."""
    before = Identity.from_fd(fd)
    digest = hashlib.sha256()
    offset_bytes = 0
    while offset_bytes < before.size_bytes:
        chunk = os.pread(fd, min(1024 * 1024, before.size_bytes - offset_bytes), offset_bytes)
        if not chunk:
            raise OSError("file became shorter while it was hashed")
        digest.update(chunk)
        offset_bytes += len(chunk)
    if Identity.from_fd(fd) != before:
        raise OSError("file identity drifted while it was hashed")
    return digest.digest()


def entry_identity(parent_fd: int, name: str) -> Identity:
    return Identity.from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))


def _clean_components(path: Path) -> tuple[str, ...]:
    anchor = path.anchor
    components = tuple(part for part in path.parts if part not in (anchor, ""))
    if any(part in (".", "..") or "/" in part for part in components):
        raise ValueError("path traversal components are forbidden")
    return components


def _clean_relative_components(path: PurePath) -> tuple[str, ...]:
    if path.is_absolute():
        raise ValueError("source file path must be relative")
    components = tuple(path.parts)
    if not components or any(part in ("", ".", "..") or "/" in part for part in components):
        raise ValueError("source file path is invalid")
    return components


def _identity_matches(current: Identity, expected: Identity, exact_metadata: bool) -> bool:
    if exact_metadata:
        return current == expected
    return (
        current.same_object(expected)
        and current.mode == expected.mode
        and current.uid == expected.uid
        and current.gid == expected.gid
    )
