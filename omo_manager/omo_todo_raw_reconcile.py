#!/usr/bin/env python3
"""Remove one obsolete global TODO line after digest-bound terminal verification."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import secrets
import stat
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_stop import has_close_note
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata

TASK_NAME = "eda_reg_chat.md"
TASK_TARGET = "hcppb:1"
RAW_TODO_LINE = 'read the last "[wl:1] Research candidate verifiers for C++ for vl" and ask for elaboration'
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SESSION_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
IN_ATTRIB = 0x00000004
IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_DONT_FOLLOW = 0x02000000
IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0x800)
IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0x80000)


class ReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Args:
    root: Path
    expected_todo_sha256: str
    expected_task_sha256: str
    close_session_id: str
    recovery_root: Path = Path(tempfile.gettempdir()) / "omo-manager-recovery"


@dataclass(frozen=True)
class PathSnapshot:
    state: os.stat_result
    payload: bytes | None


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    recovery_root: Path = Path()
    expected_todo_sha256: str = ""
    expected_task_sha256: str = ""
    close_session_id: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--recovery-root", type=Path, required=True)
    _ = parser.add_argument("--expected-todo-sha256", required=True)
    _ = parser.add_argument("--expected-task-sha256", required=True)
    _ = parser.add_argument("--close-session-id", required=True)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    todo_sha256 = parsed.expected_todo_sha256.strip()
    task_sha256 = parsed.expected_task_sha256.strip()
    session_id = parsed.close_session_id.strip()
    if SHA256_RE.fullmatch(todo_sha256) is None or SHA256_RE.fullmatch(task_sha256) is None:
        parser.error("expected TODO and task digests must be lowercase SHA-256 values")
    if SESSION_RE.fullmatch(session_id) is None:
        parser.error("close session id must be a canonical lowercase UUID")
    return Args(parsed.root.resolve(), todo_sha256, task_sha256, session_id, parsed.recovery_root.resolve())


def regular_file_state(path: Path) -> os.stat_result:
    try:
        state = path.lstat()
    except OSError as exc:
        raise ReconcileError(f"required file is unavailable: {path.name}") from exc
    if not stat.S_ISREG(state.st_mode):
        raise ReconcileError(f"required path is not a regular file: {path.name}")
    return state


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
        and left.st_size == right.st_size
    )


def same_exchanged_state(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare metadata preserved by rename exchange; Linux updates inode ctime."""
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_size == right.st_size
    )


def same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)


def recovery_scope_for(todo: Path, recovery_root: Path | None = None) -> Path:
    scope = hashlib.sha256(os.fsencode(todo.parent.resolve())).hexdigest()[:24]
    return (recovery_root or Path(tempfile.gettempdir()) / "omo-manager-recovery") / scope


def private_directory_fd(path: Path, expected_device: int) -> int:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    state = os.fstat(descriptor)
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700 or state.st_dev != expected_device:
        os.close(descriptor)
        raise ReconcileError(f"unsafe private recovery directory: {path}")
    return descriptor


def child_private_directory_fd(parent_fd: int, name: str, expected_device: int) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    state = os.fstat(descriptor)
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700 or state.st_dev != expected_device:
        os.close(descriptor)
        raise ReconcileError(f"unsafe private recovery child: {name}")
    return descriptor


def pinned_recovery_path(directory_fd: int, name: str) -> Path:
    actual_directory = Path(os.readlink(f"/proc/self/fd/{directory_fd}"))
    if not same_file_state(os.fstat(directory_fd), actual_directory.lstat()):
        raise ReconcileError("pinned recovery storage has no verified discoverable path")
    return actual_directory / name


def path_snapshot(path: Path) -> PathSnapshot:
    before = path.lstat()
    payload = path.read_bytes() if stat.S_ISREG(before.st_mode) else None
    after = path.lstat()
    if not same_file_state(before, after):
        raise ReconcileError(f"{path.name} changed while its recovery snapshot was read")
    return PathSnapshot(after, payload)


def matches_exchanged_snapshot(path: Path, snapshot: PathSnapshot) -> bool:
    try:
        current = path.lstat()
        return same_exchanged_state(snapshot.state, current) and (snapshot.payload is None or path.read_bytes() == snapshot.payload)
    except OSError:
        return False


def matches_strict_snapshot(path: Path, snapshot: PathSnapshot) -> bool:
    try:
        current = path.lstat()
        return same_file_state(snapshot.state, current) and (snapshot.payload is None or path.read_bytes() == snapshot.payload)
    except OSError:
        return False


def same_snapshot(left: PathSnapshot, right: PathSnapshot) -> bool:
    return same_exchanged_state(left.state, right.state) and left.payload == right.payload


def same_strict_snapshot(left: PathSnapshot, right: PathSnapshot) -> bool:
    return same_file_state(left.state, right.state) and left.payload == right.payload


class _MetadataWatch:
    """Watch exchange operands and optional witness paths through validation."""

    def __init__(self, paths: tuple[Path, ...], *, identity_paths: tuple[Path, ...] = ()) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            init = libc.inotify_init1
            add = libc.inotify_add_watch
        except AttributeError as exc:
            raise ReconcileError("metadata race detection is unavailable") from exc
        init.argtypes = (ctypes.c_int,)
        init.restype = ctypes.c_int
        add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        add.restype = ctypes.c_int
        fd = int(init(IN_NONBLOCK | IN_CLOEXEC))  # pyright: ignore[reportAny]
        if fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self.fd: int = fd
        self.watches: list[int] = []
        try:
            for path, mask in (
                *((path, IN_ATTRIB | IN_MODIFY | IN_CLOSE_WRITE | IN_DONT_FOLLOW) for path in paths),
                *((path, IN_ATTRIB | IN_MODIFY | IN_CLOSE_WRITE | IN_DELETE_SELF | IN_MOVE_SELF | IN_DONT_FOLLOW) for path in identity_paths),
            ):
                wd = int(add(fd, os.fsencode(path), mask))  # pyright: ignore[reportAny]
                if wd < 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error), str(path))
                self.watches.append(wd)
            _ = self._drain()
        except BaseException:
            os.close(fd)
            raise

    def _drain(self) -> bool:
        changed = False
        while True:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            if not data:
                break
            # Every event in this watch uses the metadata/content mask.
            changed = True
        return changed

    def changed(self) -> bool:
        return self._drain()

    def finish(self) -> bool:
        try:
            return self._drain()
        finally:
            self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def watch_completion(watch: _MetadataWatch) -> tuple[bool, bool]:
    """Return (change detected, completion raised an ordinary exception)."""
    try:
        return watch.finish(), False
    except Exception:
        return True, True


def watch_changed(watch: _MetadataWatch) -> bool:
    return watch_completion(watch)[0]


def verified_task(task_bytes: bytes, root: Path, session_id: str) -> None:
    try:
        text = task_bytes.decode("utf-8")
        metadata = parse_task_metadata(text, root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise ReconcileError("terminal task evidence is malformed") from exc
    if metadata is None or metadata.status != "done" or metadata.runat != TASK_TARGET or metadata.is_manager or metadata.pending_task_items:
        raise ReconcileError("terminal task must be one done non-manager at hcppb:1 with an empty queue")
    if any(line.strip() == "(pending)" for line in text.splitlines()):
        raise ReconcileError("terminal task still contains a pending delivery marker")
    if not has_close_note(text, TASK_TARGET, session_id):
        raise ReconcileError("terminal task lacks the exact close-session evidence")


def updated_todo(todo_bytes: bytes) -> bytes:
    try:
        text = todo_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReconcileError("TODO is not valid UTF-8") from exc
    lines = text.splitlines(keepends=True)
    section = ""
    human_pending_headers = 0
    matches: list[int] = []
    task_rows: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        raw = line.rstrip("\r\n")
        if raw == "human pending:":
            section = "human pending"
            human_pending_headers += 1
        elif raw.endswith(":"):
            section = raw[:-1]
        elif section == "human pending" and raw == RAW_TODO_LINE:
            matches.append(index)
        if TASK_NAME in raw:
            task_rows.append((section, raw))
    if human_pending_headers != 1 or len(matches) != 1:
        raise ReconcileError("TODO must contain one canonical human-pending section and one exact stale raw line")
    if task_rows != [("previous", f"{TASK_NAME} {TASK_TARGET}")]:
        raise ReconcileError("TODO must contain one eda_reg_chat.md row under previous at hcppb:1")
    del lines[matches[0]]
    return "".join(lines).encode("utf-8")


def _atomic_exchange_syscall(left: Path, right: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError("atomic path exchange is unavailable") from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(right))


def _rename_noreplace(source: str, destination: str, *, source_fd: int, destination_fd: int) -> None:
    """Publish one recovery entry without replacing any existing directory entry."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError("no-replace recovery publication is unavailable") from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def publish_recovery(source_fd: int, destination_fd: int, scope_name: str) -> str:
    """Move replacement to a unique recovery name without unlinking collisions."""
    for _ in range(128):
        recovery_name = f"todo-raw-reconcile-recovery-{scope_name}-{secrets.token_hex(16)}"
        try:
            _rename_noreplace("replacement", recovery_name, source_fd=source_fd, destination_fd=destination_fd)
        except FileExistsError:
            continue
        return recovery_name
    raise ReconcileError("could not allocate a collision-free recovery path")


def rename_exchange(
    left: Path,
    right: Path,
    expected_left: PathSnapshot | None = None,
    expected_right: PathSnapshot | None = None,
    *,
    witness: Path | None = None,
) -> _MetadataWatch:
    """Atomically exchange two paths without a check-to-rename gap."""
    watch = _MetadataWatch((left, right), identity_paths=() if witness is None else (witness,))
    try:
        if expected_left is not None and not matches_strict_snapshot(left, expected_left):
            raise ReconcileError("left exchange operand changed before atomic exchange")
        if expected_right is not None and not matches_strict_snapshot(right, expected_right):
            raise ReconcileError("right exchange operand changed before atomic exchange")
        _atomic_exchange_syscall(left, right)
    except BaseException:
        watch.close()
        raise
    return watch


def replace_if_unchanged(
    path: Path,
    payload: bytes,
    source: bytes,
    before: os.stat_result,
    witness: Path,
    witness_source: bytes,
    witness_before: os.stat_result,
    recovery_root: Path,
) -> None:
    temporary: Path | None = None
    recovery_name: str | None = None
    root_fd: int | None = None
    recovery_root_fd: int | None = None
    recovery_scope_fd: int | None = None
    private_fd: int | None = None

    def recovery_path_now() -> Path:
        assert recovery_root_fd is not None
        assert recovery_name is not None
        return pinned_recovery_path(recovery_root_fd, recovery_name)

    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(path.parent, directory_flags)
        todo_path = Path(f"/proc/self/fd/{root_fd}/{path.name}")
        try:
            _ = recovery_root.resolve().relative_to(path.parent.resolve())
        except ValueError:
            pass
        else:
            raise ReconcileError("private recovery storage must be outside the work-log root")
        recovery_root_fd = private_directory_fd(recovery_root, before.st_dev)
        recovery_scope = recovery_scope_for(path, recovery_root)
        recovery_scope_fd = child_private_directory_fd(recovery_root_fd, recovery_scope.name, before.st_dev)
        private_name = f"todo-raw-reconcile-{secrets.token_hex(16)}"
        os.mkdir(private_name, 0o700, dir_fd=recovery_scope_fd)
        private_fd = child_private_directory_fd(recovery_scope_fd, private_name, before.st_dev)
        private_dir = recovery_scope / private_name
        os.fchmod(private_fd, 0o700)
        private_state = os.fstat(private_fd)
        if not stat.S_ISDIR(private_state.st_mode) or private_state.st_uid != os.getuid() or stat.S_IMODE(private_state.st_mode) != 0o700:
            raise ReconcileError(f"unsafe private recovery directory retained at {private_dir}")
        file_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open("replacement", file_flags, 0o600, dir_fd=private_fd)
        temporary = Path(f"/proc/self/fd/{private_fd}/replacement")
        with os.fdopen(descriptor, "w+b") as handle:
            _ = handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), before.st_mode & 0o7777)
            if not same_file_state(os.fstat(handle.fileno()), regular_file_state(temporary)):
                raise ReconcileError("private TODO replacement path changed before reconciliation")
            _ = handle.seek(0)
            if handle.read() != payload:
                raise ReconcileError("private TODO replacement bytes changed before reconciliation")
            if not same_file_state(before, regular_file_state(todo_path)) or todo_path.read_bytes() != source:
                raise ReconcileError("TODO changed while reconciliation was prepared; retry with fresh digests")
            if not same_file_state(witness_before, regular_file_state(witness)) or witness.read_bytes() != witness_source:
                raise ReconcileError("terminal task changed while reconciliation was prepared")
            replacement_snapshot = path_snapshot(temporary)
            todo_snapshot = path_snapshot(todo_path)
            try:
                exchange_watch = rename_exchange(temporary, todo_path, replacement_snapshot, todo_snapshot, witness=witness)
            except (OSError, ReconcileError) as exc:
                assert recovery_root_fd is not None
                recovery_name = publish_recovery(private_fd, recovery_root_fd, recovery_scope.name)
                temporary = None
                raise ReconcileError(f"exchange operand changed; recovery path {recovery_path_now()}") from exc
            assert recovery_root_fd is not None
            publication_todo_snapshot = path_snapshot(todo_path)
            publication_private_snapshot = path_snapshot(temporary)
            try:
                recovery_name = publish_recovery(private_fd, recovery_root_fd, recovery_scope.name)
            except (OSError, ReconcileError) as exc:
                # The exchange already changed TODO.  Roll back only if both
                # pinned operands still contain exactly the exchanged bytes;
                # otherwise leave the current TODO untouched and report the
                # still-discoverable private recovery path.
                private_recovery_path = pinned_recovery_path(private_fd, "replacement")
                try:
                    # Completion errors count as detected change, but—as in
                    # the normal post-exchange path—fresh verified operands
                    # may still be rolled back safely.
                    try:
                        publication_watch_event = exchange_watch.changed()
                    except Exception:
                        publication_watch_event = True
                    publication_watch_changed, publication_watch_failed = watch_completion(exchange_watch)
                    post_watch_todo = path_snapshot(todo_path)
                    post_watch_private = path_snapshot(temporary)
                    if publication_watch_event or (publication_watch_changed and not publication_watch_failed):
                        raise ReconcileError("metadata changed while recovery publication failed")
                    if not same_strict_snapshot(publication_todo_snapshot, post_watch_todo) or not same_strict_snapshot(publication_private_snapshot, post_watch_private):
                        raise ReconcileError("exchange operands changed while recovery publication failed")
                    private_snapshot = path_snapshot(temporary)
                    installed_snapshot = path_snapshot(todo_path)
                    rollback_watch = rename_exchange(temporary, todo_path, private_snapshot, installed_snapshot)
                    if watch_changed(rollback_watch):
                        raise ReconcileError("metadata changed while recovery publication rollback completed")
                    if not matches_exchanged_snapshot(todo_path, private_snapshot) or not matches_exchanged_snapshot(temporary, installed_snapshot):
                        raise ReconcileError("recovery publication rollback validation failed")
                except (OSError, ReconcileError) as rollback_exc:
                    temporary = None
                    raise ReconcileError(f"recovery publication failed; recovery evidence retained at {private_recovery_path}") from rollback_exc
                temporary = None
                raise ReconcileError(f"recovery publication failed after exchange; TODO restored and recovery evidence retained at {private_recovery_path}") from exc
            temporary = Path(f"/proc/self/fd/{recovery_root_fd}/{recovery_name}")
            installed_is_ours = False
            displaced_is_source = False
            witness_is_source = False
            try:
                _ = handle.seek(0)
                installed = regular_file_state(todo_path)
                displaced = regular_file_state(temporary)
                installed_is_ours = (
                    same_file_state(os.fstat(handle.fileno()), installed)
                    and same_exchanged_state(replacement_snapshot.state, installed)
                    and handle.read() == payload
                    and todo_path.read_bytes() == payload
                )
                displaced_is_source = same_exchanged_state(before, displaced) and temporary.read_bytes() == source
                witness_is_source = same_file_state(witness_before, regular_file_state(witness)) and witness.read_bytes() == witness_source
            except (OSError, ReconcileError):
                try:
                    displaced = regular_file_state(temporary)
                    displaced_is_source = same_exchanged_state(before, displaced) and temporary.read_bytes() == source
                except (OSError, ReconcileError):
                    pass
            try:
                installed_validation_snapshot = path_snapshot(todo_path)
                displaced_validation_snapshot = path_snapshot(temporary)
            except (OSError, ReconcileError):
                installed_validation_snapshot = None
                displaced_validation_snapshot = None
            exchange_metadata_changed = watch_changed(exchange_watch)
            try:
                installed_after_finish = path_snapshot(todo_path)
                displaced_after_finish = path_snapshot(temporary)
                installed_unchanged = installed_validation_snapshot is not None and same_strict_snapshot(installed_validation_snapshot, installed_after_finish)
                displaced_unchanged = displaced_validation_snapshot is not None and same_strict_snapshot(displaced_validation_snapshot, displaced_after_finish)
            except (OSError, ReconcileError):
                installed_unchanged = False
                displaced_unchanged = False
            installed_is_ours = installed_is_ours and installed_unchanged
            displaced_is_source = displaced_is_source and displaced_unchanged
            try:
                witness_is_source_after_finish = same_file_state(witness_before, regular_file_state(witness)) and witness.read_bytes() == witness_source
            except (OSError, ReconcileError):
                witness_is_source_after_finish = False
            if exchange_metadata_changed or not (installed_is_ours and displaced_is_source and witness_is_source and witness_is_source_after_finish):
                rollback_is_safe = displaced_is_source and installed_is_ours and displaced_unchanged
                if not rollback_is_safe:
                    temporary = None
                    raise ReconcileError(f"concurrent TODO change preserved at the atomic reconciliation boundary; recovery path {recovery_path_now()}")
                installed_snapshot = path_snapshot(todo_path)
                displaced_snapshot = path_snapshot(temporary)
                try:
                    rollback_watch = rename_exchange(temporary, todo_path, displaced_snapshot, installed_snapshot)
                except (OSError, ReconcileError) as exc:
                    temporary = None
                    raise ReconcileError(f"concurrent TODO retained at recovery path {recovery_path_now()}; rollback exchange was not committed") from exc
                rollback_valid = matches_exchanged_snapshot(todo_path, displaced_snapshot) and matches_exchanged_snapshot(temporary, installed_snapshot)
                rollback_metadata_changed = watch_changed(rollback_watch)
                if rollback_metadata_changed or not rollback_valid:
                    rollback_path_snapshot = path_snapshot(todo_path)
                    rollback_temp_snapshot = path_snapshot(temporary)
                    try:
                        recovery_watch = rename_exchange(temporary, todo_path, rollback_temp_snapshot, rollback_path_snapshot)
                    except (OSError, ReconcileError) as exc:
                        temporary = None
                        raise ReconcileError(f"concurrent TODO retained at recovery path {recovery_path_now()}; restore exchange failed") from exc
                    recovery_valid = matches_exchanged_snapshot(todo_path, rollback_temp_snapshot) and matches_exchanged_snapshot(temporary, rollback_path_snapshot)
                    recovery_metadata_changed = watch_changed(recovery_watch)
                    recovery_valid = not recovery_metadata_changed and recovery_valid
                    if not recovery_valid:
                        temporary = None
                        raise ReconcileError(f"repeated concurrent TODO changes retained at TODO.md and recovery path {recovery_path_now()}")
                    if not same_snapshot(rollback_path_snapshot, displaced_snapshot):
                        temporary = None
                        raise ReconcileError(f"rollback-path substitution retained at recovery path {recovery_path_now()}")
                    temporary = None
                    raise ReconcileError("concurrent TODO change preserved during atomic rollback")
                raise ReconcileError("TODO or terminal task changed at the atomic reconciliation boundary")
        final_recovery_path = recovery_path_now()
        if final_recovery_path.parent != recovery_root.resolve():
            raise ReconcileError(f"recovery root moved; retained recovery path {final_recovery_path}")
        temporary = None
    finally:
        if private_fd is not None:
            os.close(private_fd)
        if recovery_scope_fd is not None:
            os.close(recovery_scope_fd)
        if recovery_root_fd is not None:
            os.close(recovery_root_fd)
        if root_fd is not None:
            os.close(root_fd)


def reconcile(args: Args) -> None:
    root = args.root.resolve()
    todo = root / "TODO.md"
    task = root / TASK_NAME
    with ExitStack() as locks:
        for path in sorted((task, todo), key=str):
            locks.enter_context(task_file_lock(path))
        todo_before = regular_file_state(todo)
        task_before = regular_file_state(task)
        todo_bytes = todo.read_bytes()
        task_bytes = task.read_bytes()
        if hashlib.sha256(todo_bytes).hexdigest() != args.expected_todo_sha256:
            raise ReconcileError("TODO digest is stale")
        if hashlib.sha256(task_bytes).hexdigest() != args.expected_task_sha256:
            raise ReconcileError("terminal task digest is stale")
        verified_task(task_bytes, root, args.close_session_id)
        replacement = updated_todo(todo_bytes)
        if not same_file_state(task_before, regular_file_state(task)) or task.read_bytes() != task_bytes:
            raise ReconcileError("terminal task changed while reconciliation was prepared")
        replace_if_unchanged(todo, replacement, todo_bytes, todo_before, task, task_bytes, task_before, args.recovery_root)


def main(argv: list[str] | None = None) -> int:
    try:
        reconcile(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, ReconcileError) as exc:
        print(f"omo_todo_raw_reconcile.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
