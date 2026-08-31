"""Provision manifests and freestanding launchers for a sealed Linux runtime root."""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from ._bindings import (
    BoundDirectory,
    Identity,
    bind_directory,
    digest_fd,
    entry_identity,
    open_regular_beneath,
)

CAPABILITY_ID = "sealed-bootstrap-linux-x86_64-v1"
MANIFEST_VERSION = 1
_MAGIC = b"LPCBOOTSTRAP\x00\x01\x00\x00"
_HEADER = struct.Struct("<16s5I")
_U32 = struct.Struct("<I")
_IDENTITY = struct.Struct("<8Q2q")
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_NATIVE_SOURCE = Path(__file__).with_name("_sealed_launcher.c")
_FORBIDDEN_ENVIRONMENT_NAMES = (
    "BASH_ENV",
    "BASHOPTS",
    "CDPATH",
    "CLASSPATH",
    "ENV",
    "GCONV_PATH",
    "GLIBC_TUNABLES",
    "IFS",
    "JAVA_TOOL_OPTIONS",
    "LOCPATH",
    "NODE_OPTIONS",
    "PERL5OPT",
    "RUBYOPT",
    "SHELLOPTS",
)
_FORBIDDEN_ENVIRONMENT_PREFIXES = ("LD_", "PYTHON")


@dataclass(frozen=True, slots=True)
class RootSpec:
    """An absolute source root retained while its selected files are measured."""

    source: Path


@dataclass(frozen=True, slots=True)
class FileSpec:
    """One authenticated regular file materialized at an absolute runtime path."""

    root_index: int
    source: PurePosixPath
    destination: PurePosixPath
    mode: int = 0o444


@dataclass(frozen=True, slots=True)
class EnvironmentEntry:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class BootstrapSpec:
    launch_directory: Path
    roots: tuple[RootSpec, ...]
    files: tuple[FileSpec, ...]
    executable: PurePosixPath
    cwd: PurePosixPath
    argv: tuple[str, ...]
    environment: tuple[EnvironmentEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestSeal:
    path: Path
    sha256: str
    identity: Identity
    capability_id: str = CAPABILITY_ID
    manifest_version: int = MANIFEST_VERSION


@dataclass(frozen=True, slots=True)
class LauncherSeal:
    path: Path
    sha256: str
    identity: Identity
    manifest_sha256: str
    capability_id: str = CAPABILITY_ID


class _ProvisionPhase(Enum):
    MANIFEST_OUTPUT_BOUND = "manifest-output-bound"
    MANIFEST_OUTPUT_DURABLE = "manifest-output-durable"
    LAUNCHER_OUTPUT_BOUND = "launcher-output-bound"
    LAUNCHER_OUTPUT_DURABLE = "launcher-output-durable"


_ProvisionHook = Callable[[_ProvisionPhase], None]


@dataclass(frozen=True, slots=True)
class _MeasuredRoot:
    source: bytes
    identities: tuple[Identity, ...]


@dataclass(frozen=True, slots=True)
class _MeasuredFile:
    root_index: int
    source: bytes
    destination: bytes
    identity: Identity
    sha256: bytes
    mode: int


def create_manifest(
    spec: BootstrapSpec,
    output: Path,
    *,
    _phase_hook: _ProvisionHook | None = None,
) -> ManifestSeal:
    """Measure exact roots/files and durably create a new immutable launch manifest."""
    _validate_spec(spec)
    launch_directory = bind_directory(spec.launch_directory, require_absolute=True)
    roots_list: list[BoundDirectory] = []
    try:
        for root in spec.roots:
            roots_list.append(bind_directory(root.source, require_absolute=True))
        roots = tuple(roots_list)
        measured_launch_directory = _MeasuredRoot(
            os.fsencode(spec.launch_directory), launch_directory.identities
        )
        measured_roots = tuple(
            _MeasuredRoot(os.fsencode(spec.roots[index].source), root.identities)
            for index, root in enumerate(roots)
        )
        measured_files: list[_MeasuredFile] = []
        for file in spec.files:
            fd = open_regular_beneath(roots[file.root_index].fd, file.source)
            try:
                identity = Identity.from_fd(fd)
                digest = digest_fd(fd)
                if file.mode & 0o111:
                    _assert_runtime_elf(fd)
                if Identity.from_fd(fd) != identity or not entry_identity(
                    roots[file.root_index].fd, os.fspath(file.source)
                ).same_object(identity):
                    raise OSError("a source file drifted while the manifest was created")
                measured_files.append(
                    _MeasuredFile(
                        file.root_index,
                        os.fsencode(file.source),
                        os.fsencode(file.destination),
                        identity,
                        digest,
                        file.mode,
                    )
                )
            finally:
                os.close(fd)
        if not launch_directory.validate() or not all(root.validate() for root in roots):
            raise OSError("a source root drifted while the manifest was created")
        value = _encode_manifest(
            spec, measured_launch_directory, measured_roots, tuple(measured_files)
        )
    finally:
        for root in roots_list:
            root.close()
        launch_directory.close()
    if len(value) > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the launcher limit")
    _validate_output_leaf(output)
    output_parent = bind_directory(output.parent)
    fd: int | None = None
    try:
        _call_provision_hook(_phase_hook, _ProvisionPhase.MANIFEST_OUTPUT_BOUND)
        fd = os.open(
            output.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
            dir_fd=output_parent.fd,
        )
        _write_all(fd, value)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        identity = Identity.from_fd(fd)
        os.fsync(output_parent.fd)
        _call_provision_hook(_phase_hook, _ProvisionPhase.MANIFEST_OUTPUT_DURABLE)
        if (
            not output_parent.validate()
            or not entry_identity(output_parent.fd, output.name).same_object(identity)
            or Identity.from_fd(fd) != identity
            or digest_fd(fd) != hashlib.sha256(value).digest()
        ):
            raise OSError("manifest output binding drifted during durable creation")
    finally:
        if fd is not None:
            os.close(fd)
        output_parent.close()
    return ManifestSeal(output, hashlib.sha256(value).hexdigest(), identity)


def build_launcher(
    manifest: ManifestSeal,
    output: Path,
    *,
    compiler: Path = Path("/usr/bin/gcc"),
) -> LauncherSeal:
    """Compile a manifest-bound launcher with no interpreter or dynamic loader."""
    return _build_launcher(manifest, output, compiler=compiler)


def _build_launcher(
    manifest: ManifestSeal,
    output: Path,
    *,
    compiler: Path = Path("/usr/bin/gcc"),
    test_pause_phase: int | None = None,
    test_pause_file_index: int = 0,
    test_binfmt_flags: str | None = None,
    _phase_hook: _ProvisionHook | None = None,
) -> LauncherSeal:
    _verify_manifest_seal(manifest)
    compiler_path = compiler.resolve(strict=True)
    if not compiler_path.is_file() or not os.access(compiler_path, os.X_OK):
        raise ValueError("compiler must be an executable regular file")
    _validate_output_leaf(output)
    output_parent = bind_directory(output.parent)
    temporary_fd: int | None = None
    try:
        _call_provision_hook(_phase_hook, _ProvisionPhase.LAUNCHER_OUTPUT_BOUND)
        temporary_fd = os.open(
            ".",
            os.O_RDWR | os.O_TMPFILE | os.O_CLOEXEC,
            0o600,
            dir_fd=output_parent.fd,
        )
        temporary_path = Path(f"/proc/self/fd/{temporary_fd}")
        command = [
            os.fspath(compiler_path),
            "-std=c17",
            "-Os",
            "-static",
            "-nostdlib",
            "-ffreestanding",
            "-fno-stack-protector",
            "-fno-builtin",
            "-fno-ident",
            "-fno-asynchronous-unwind-tables",
            "-fno-unwind-tables",
            "-Werror",
            "-Wall",
            "-Wextra",
            "-Wl,--build-id=none",
            "-Wl,-z,noexecstack",
            "-Wl,-e,_start",
            f'-DSB_MANIFEST_SHA256_HEX="{manifest.sha256}"',
            f"-DSB_MANIFEST_DEV_MAJOR={manifest.identity.device_major}ULL",
            f"-DSB_MANIFEST_DEV_MINOR={manifest.identity.device_minor}ULL",
            f"-DSB_MANIFEST_INODE={manifest.identity.inode}ULL",
            f"-DSB_MANIFEST_MODE={manifest.identity.mode}ULL",
            f"-DSB_MANIFEST_UID={manifest.identity.uid}ULL",
            f"-DSB_MANIFEST_GID={manifest.identity.gid}ULL",
            f"-DSB_MANIFEST_NLINK={manifest.identity.link_count}ULL",
            f"-DSB_MANIFEST_SIZE={manifest.identity.size_bytes}ULL",
            f"-DSB_MANIFEST_MTIME_NS={manifest.identity.modified_ns}LL",
            f"-DSB_MANIFEST_CTIME_NS={manifest.identity.changed_ns}LL",
        ]
        if test_pause_phase is not None:
            command.extend(
                (
                    f"-DSB_TEST_PAUSE_PHASE={test_pause_phase}",
                    f"-DSB_TEST_PAUSE_FILE_INDEX={test_pause_file_index}",
                )
            )
        if test_binfmt_flags is not None:
            if not test_binfmt_flags.isascii() or not test_binfmt_flags.isalpha():
                raise ValueError("test binfmt flags must contain only ASCII letters")
            command.append(f'-DSB_TEST_BINFMT_FLAGS="{test_binfmt_flags}"')
        command.extend((os.fspath(_NATIVE_SOURCE), "-o", os.fspath(temporary_path)))
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            pass_fds=(temporary_fd,),
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
        if completed.returncode != 0:
            raise OSError(f"freestanding launcher build failed: {completed.stderr.strip()}")
        _assert_freestanding_elf(temporary_fd)
        os.fchmod(temporary_fd, 0o500)
        os.fsync(temporary_fd)
        _verify_manifest_seal(manifest)
        os.link(
            os.fspath(temporary_path),
            output.name,
            dst_dir_fd=output_parent.fd,
            follow_symlinks=True,
        )
        os.fsync(temporary_fd)
        os.fsync(output_parent.fd)
        identity = Identity.from_fd(temporary_fd)
        launcher_digest = digest_fd(temporary_fd).hex()
        _call_provision_hook(_phase_hook, _ProvisionPhase.LAUNCHER_OUTPUT_DURABLE)
        if (
            not output_parent.validate()
            or not entry_identity(output_parent.fd, output.name).same_object(identity)
            or Identity.from_fd(temporary_fd) != identity
            or digest_fd(temporary_fd).hex() != launcher_digest
        ):
            raise OSError("launcher output binding drifted during durable creation")
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        output_parent.close()
    return LauncherSeal(output, launcher_digest, identity, manifest.sha256)


def _validate_spec(spec: BootstrapSpec) -> None:
    if not spec.roots:
        raise ValueError("at least one source root is required")
    if not spec.files:
        raise ValueError("at least one runtime file is required")
    if not spec.argv:
        raise ValueError("argv must not be empty")
    if not spec.launch_directory.is_absolute():
        raise ValueError("launch directory must be absolute")
    destinations: set[PurePosixPath] = set()
    executable_mode: int | None = None
    for root in spec.roots:
        if not root.source.is_absolute():
            raise ValueError("source roots must be absolute")
    for file in spec.files:
        if file.root_index < 0 or file.root_index >= len(spec.roots):
            raise ValueError("file root index is out of range")
        _validate_relative(file.source)
        _validate_absolute(file.destination)
        if file.destination in destinations:
            raise ValueError("runtime destinations must be unique")
        destinations.add(file.destination)
        if file.mode & ~0o555 or file.mode & 0o222:
            raise ValueError("runtime file modes must be read-only and may only add execute bits")
        if file.destination == spec.executable:
            executable_mode = file.mode
    _validate_absolute(spec.executable)
    _validate_absolute(spec.cwd)
    if executable_mode is None or executable_mode & 0o111 == 0:
        raise ValueError("executable must name an executable manifest file")
    names: set[str] = set()
    for item in (
        *spec.argv,
        *(entry.name for entry in spec.environment),
        *(entry.value for entry in spec.environment),
    ):
        if "\x00" in item:
            raise ValueError("argv and environment may not contain NUL")
    for entry in spec.environment:
        if not entry.name or "=" in entry.name or entry.name in names:
            raise ValueError("environment names must be unique and may not contain `=`")
        if entry.name in _FORBIDDEN_ENVIRONMENT_NAMES or entry.name.startswith(
            _FORBIDDEN_ENVIRONMENT_PREFIXES
        ):
            raise ValueError("loader and startup-hook environment names are forbidden")
        names.add(entry.name)


def _validate_relative(path: PurePosixPath) -> None:
    if (
        path.is_absolute()
        or len(path.parts) != 1
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("each source root must be the exact parent of its source file")


def _validate_absolute(path: PurePosixPath) -> None:
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or any(part in ("", ".", "..") for part in path.parts[1:])
    ):
        raise ValueError("runtime paths must be canonical, absolute, and non-root")


def _encode_manifest(
    spec: BootstrapSpec,
    launch_directory: _MeasuredRoot,
    roots: tuple[_MeasuredRoot, ...],
    files: tuple[_MeasuredFile, ...],
) -> bytes:
    parts = [
        _HEADER.pack(_MAGIC, len(roots), len(files), len(spec.argv), len(spec.environment), 0),
        _string(launch_directory.source),
        _U32.pack(len(launch_directory.identities)),
        *(_pack_identity(identity) for identity in launch_directory.identities),
        _string(os.fsencode(spec.executable)),
        _string(os.fsencode(spec.cwd)),
    ]
    for root in roots:
        parts.extend((_string(root.source), _U32.pack(len(root.identities))))
        parts.extend(_pack_identity(identity) for identity in root.identities)
    for file in files:
        parts.extend(
            (
                _U32.pack(file.root_index),
                _string(file.source),
                _string(file.destination),
                _pack_identity(file.identity),
                file.sha256,
                _U32.pack(file.mode),
            )
        )
    parts.extend(_string(value.encode()) for value in spec.argv)
    parts.extend(_string(f"{entry.name}={entry.value}".encode()) for entry in spec.environment)
    return b"".join(parts)


def _string(value: bytes) -> bytes:
    if b"\x00" in value or len(value) > 4095:
        raise ValueError("manifest string is invalid")
    return _U32.pack(len(value)) + value + b"\x00"


def _pack_identity(identity: Identity) -> bytes:
    return _IDENTITY.pack(
        identity.device_major,
        identity.device_minor,
        identity.inode,
        identity.mode,
        identity.uid,
        identity.gid,
        identity.link_count,
        identity.size_bytes,
        identity.modified_ns,
        identity.changed_ns,
    )


def _verify_manifest_seal(manifest: ManifestSeal) -> None:
    if manifest.capability_id != CAPABILITY_ID or manifest.manifest_version != MANIFEST_VERSION:
        raise ValueError("unsupported manifest capability/version")
    _validate_output_leaf(manifest.path)
    parent = bind_directory(manifest.path.parent)
    fd: int | None = None
    try:
        fd = os.open(
            manifest.path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent.fd,
        )
        if (
            Identity.from_fd(fd) != manifest.identity
            or digest_fd(fd).hex() != manifest.sha256
            or not parent.validate()
            or not entry_identity(parent.fd, manifest.path.name).same_object(manifest.identity)
        ):
            raise OSError("manifest seal no longer matches its descriptor identity")
    finally:
        if fd is not None:
            os.close(fd)
        parent.close()


def _validate_output_leaf(path: Path) -> None:
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise ValueError("output must be an absolute path with a canonical leaf name")


def _assert_freestanding_elf(fd: int) -> None:
    try:
        _assert_runtime_elf(fd)
    except ValueError as error:
        raise OSError(str(error)) from error
    header = os.pread(fd, 64, 0)
    program_offset = struct.unpack_from("<Q", header, 32)[0]
    entry_size = struct.unpack_from("<H", header, 54)[0]
    entry_count = struct.unpack_from("<H", header, 56)[0]
    if entry_size < 56 or entry_count > 128:
        raise OSError("launcher program-header table is invalid")
    for index in range(entry_count):
        entry = os.pread(fd, entry_size, program_offset + index * entry_size)
        if len(entry) != entry_size:
            raise OSError("launcher program-header table is truncated")
        if struct.unpack_from("<I", entry)[0] in (2, 3):
            raise OSError("launcher contains a dynamic section or interpreter")


def _assert_runtime_elf(fd: int) -> None:
    header = os.pread(fd, 64, 0)
    size_bytes = os.fstat(fd).st_size
    if (
        len(header) != 64
        or header[:4] != b"\x7fELF"
        or header[4:7] != b"\x02\x01\x01"
        or struct.unpack_from("<H", header, 16)[0] not in (2, 3)
        or struct.unpack_from("<H", header, 18)[0] != 62
        or struct.unpack_from("<I", header, 20)[0] != 1
        or struct.unpack_from("<H", header, 52)[0] < 64
    ):
        raise ValueError("every execute-bit runtime file must be a Linux x86-64 ELF binary")
    program_offset = struct.unpack_from("<Q", header, 32)[0]
    entry_size = struct.unpack_from("<H", header, 54)[0]
    entry_count = struct.unpack_from("<H", header, 56)[0]
    if (
        entry_size < 56
        or entry_count == 0
        or entry_count > 128
        or program_offset > size_bytes
        or entry_count > (size_bytes - program_offset) // entry_size
    ):
        raise ValueError("execute-bit runtime ELF program-header table is invalid")


def _call_provision_hook(hook: _ProvisionHook | None, phase: _ProvisionPhase) -> None:
    if hook is not None:
        hook(phase)


def _write_all(fd: int, value: bytes) -> None:
    offset_bytes = 0
    while offset_bytes < len(value):
        offset_bytes += os.write(fd, value[offset_bytes:])
