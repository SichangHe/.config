#!/usr/bin/env python3
"""Create an atomic record from explicitly supplied experiment files."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

SCHEMA = "omo-experiment-record/v1"
SCOPE_STATEMENT = "This record preserves only explicitly supplied files and does not establish global transcript completeness."
TOKEN_SOURCE = "Codex JSONL payload.info.total_token_usage"
TOKEN_AGGREGATION = (
    "Cumulative counters are ordered by timestamp and aggregated per Codex session; duplicates add zero, all-counter decreases start a new segment, and mixed decreases add only positive field deltas."
)
RENAME_NOREPLACE = 1


class RecordError(ValueError):
    """A record cannot be safely created from the supplied arguments."""


@dataclass(frozen=True)
class Args:
    output_dir: Path
    transcripts: tuple[Path, ...]
    prompt: Path
    inputs: tuple[Path, ...]
    started_at: str
    ended_at: str


class ParsedArgs(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.output_dir: Path = Path()
        self.transcript: list[Path] = []
        self.prompt: Path = Path()
        self.inputs: list[Path] = []
        self.started_at: str = ""
        self.ended_at: str = ""


@dataclass(frozen=True)
class FileState:
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class OutputTarget:
    output: Path
    parent_descriptor: int
    parent_identity: tuple[int, int]

    @property
    def parent_access_path(self) -> Path:
        return Path("/proc/self/fd") / str(self.parent_descriptor)


@dataclass(frozen=True)
class SourceSpec:
    role: str
    source: Path
    destination: Path


@dataclass(frozen=True)
class SourceCopy:
    role: str
    source: Path
    destination: Path
    source_state: FileState


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    def add(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    def delta_from(self, previous: TokenUsage) -> TokenUsage:
        deltas = (
            self.input_tokens - previous.input_tokens,
            self.cached_input_tokens - previous.cached_input_tokens,
            self.output_tokens - previous.output_tokens,
            self.reasoning_output_tokens - previous.reasoning_output_tokens,
        )
        if all(value >= 0 for value in deltas):
            return TokenUsage(deltas[0], deltas[1], deltas[2], deltas[3])
        if all(value <= 0 for value in deltas):
            return self
        return TokenUsage(max(0, deltas[0]), max(0, deltas[1]), max(0, deltas[2]), max(0, deltas[3]))


@dataclass(frozen=True)
class TokenSnapshot:
    timestamp: datetime
    file_index: int
    line_number: int
    session: str
    usage: TokenUsage


@dataclass(frozen=True)
class TokenReport:
    usage: TokenUsage | None
    records_found: int
    unavailable_reason: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--output-dir", required=True, type=Path, help="Initially absent artifact directory to publish.")
    _ = parser.add_argument("--transcript", required=True, action="append", type=Path, help="Transcript file to preserve; repeat for each file.")
    _ = parser.add_argument("--prompt", required=True, type=Path, help="Prompt file to preserve as a detached attachment.")
    _ = parser.add_argument("--input", dest="inputs", action="append", default=[], type=Path, help="Additional detached input attachment; repeat as needed.")
    _ = parser.add_argument("--started-at", required=True, help="Caller-supplied ISO 8601 start timestamp with an explicit timezone.")
    _ = parser.add_argument("--ended-at", required=True, help="Caller-supplied ISO 8601 end timestamp with an explicit timezone.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.output_dir, tuple(parsed.transcript), parsed.prompt, tuple(parsed.inputs), parsed.started_at, parsed.ended_at)


def parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError as exc:
        raise RecordError(f"{label} must be an ISO 8601 timestamp with an explicit timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecordError(f"{label} must include an explicit timezone")
    return parsed


def elapsed_seconds(args: Args) -> float:
    started = parse_timestamp(args.started_at, "--started-at")
    ended = parse_timestamp(args.ended_at, "--ended-at")
    elapsed = (ended.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
    if elapsed < 0:
        raise RecordError("--ended-at must not be earlier than --started-at")
    return elapsed


def open_real_directory(path: Path, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise RecordError(f"{label} must exist and contain no symlink components: {path}") from exc
    return descriptor


def open_output_target(path: Path) -> OutputTarget:
    if not path.name or ".." in path.parts:
        raise RecordError("--output-dir must name one unambiguous child directory without `..`")
    output = Path(os.path.abspath(path))
    parent_descriptor = open_real_directory(output.parent, "output parent")
    try:
        parent_state = os.fstat(parent_descriptor)
        if parent_state.st_uid != os.geteuid() or stat.S_IMODE(parent_state.st_mode) & 0o022:
            raise RecordError("output parent must be owned by the effective user and grant no group or other write permissions")
        try:
            _ = os.stat(output.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return OutputTarget(output, parent_descriptor, (parent_state.st_dev, parent_state.st_ino))
    except BaseException:
        os.close(parent_descriptor)
        raise
    os.close(parent_descriptor)
    raise RecordError(f"output directory already exists: {output}")


def revalidate_output_parent(target: OutputTarget) -> None:
    descriptor = open_real_directory(target.output.parent, "output parent")
    try:
        state = os.fstat(descriptor)
        if (state.st_dev, state.st_ino) != target.parent_identity:
            raise RecordError("output parent changed after validation")
    finally:
        os.close(descriptor)


def open_regular_descriptor(path: Path, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecordError(f"cannot open {label} as a non-symlink regular file: {path}") from exc
    state = os.fstat(descriptor)
    if stat.S_ISREG(state.st_mode):
        return descriptor, state
    os.close(descriptor)
    raise RecordError(f"{label} is not a regular file: {path}")


def file_identity(state: os.stat_result) -> tuple[int, int, int, int, int]:
    return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns


def write_all(descriptor: int, payload: bytes | memoryview) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise RecordError("artifact write was incomplete")
        written += count


def stream_file_state(path: Path, label: str, destination_descriptor: int | None = None) -> FileState:
    descriptor, before = open_regular_descriptor(path, label)
    try:
        digest = hashlib.sha256()
        size_bytes = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
            if destination_descriptor is not None:
                write_all(destination_descriptor, chunk)
        after = os.fstat(descriptor)
        if file_identity(before) != file_identity(after) or size_bytes != after.st_size:
            raise RecordError(f"{label} changed while it was read: {path}")
        return FileState(digest.hexdigest(), size_bytes)
    finally:
        os.close(descriptor)


def read_generated_bytes(path: Path, label: str) -> bytes:
    descriptor, before = open_regular_descriptor(path, label)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        if file_identity(before) != file_identity(os.fstat(descriptor)):
            raise RecordError(f"{label} changed while it was read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def source_specs(args: Args) -> tuple[SourceSpec, ...]:
    requested = [
        *(("transcript", path, Path("transcripts") / path.name) for path in args.transcripts),
        ("prompt", args.prompt, Path("attachments/prompt") / args.prompt.name),
        *(("input", path, Path("attachments/inputs") / path.name) for path in args.inputs),
    ]
    basenames: dict[str, Path] = {}
    for _, source, _ in requested:
        if not source.name:
            raise RecordError(f"supplied file path has no basename: {source}")
        previous = basenames.get(source.name)
        if previous is not None:
            raise RecordError(f"duplicate supplied basename `{source.name}`: {previous} and {source}")
        basenames[source.name] = source
    specs: list[SourceSpec] = []
    for role, source, destination in requested:
        absolute_source = Path(os.path.abspath(source))
        descriptor, _ = open_regular_descriptor(absolute_source, f"{role} source")
        os.close(descriptor)
        specs.append(SourceSpec(role, absolute_source, destination))
    return tuple(specs)


def string_keyed_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapping = cast(dict[object, object], value)
    return {key: item for key, item in mapping.items() if isinstance(key, str)}


def nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def token_usage(value: object) -> TokenUsage | None:
    fields = string_keyed_dict(value)
    if fields is None:
        return None
    input_tokens = nonnegative_int(fields.get("input_tokens"))
    cached_input_tokens = nonnegative_int(fields.get("cached_input_tokens"))
    output_tokens = nonnegative_int(fields.get("output_tokens"))
    reasoning_output_tokens = nonnegative_int(fields.get("reasoning_output_tokens"))
    if None in {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}:
        return None
    assert input_tokens is not None and cached_input_tokens is not None and output_tokens is not None and reasoning_output_tokens is not None
    return TokenUsage(input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens)


def snapshot_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def token_snapshots(staging: Path, transcripts: tuple[SourceCopy, ...]) -> tuple[list[TokenSnapshot], str]:
    snapshots: list[TokenSnapshot] = []
    for file_index, transcript in enumerate(transcripts):
        session = f"supplied-transcript:{file_index}"
        staged_transcript = staging / transcript.destination
        descriptor, before = open_regular_descriptor(staged_transcript, "staged transcript")
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        loaded = cast(object, json.loads(line))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    record = string_keyed_dict(loaded)
                    if record is None:
                        continue
                    payload = string_keyed_dict(record.get("payload"))
                    if payload is None:
                        continue
                    if record.get("type") == "session_meta":
                        session_value = payload.get("id") or payload.get("session_id")
                        if isinstance(session_value, str) and session_value:
                            session = session_value
                        continue
                    if payload.get("type") != "token_count":
                        continue
                    info = string_keyed_dict(payload.get("info"))
                    usage = token_usage(info.get("total_token_usage")) if info is not None else None
                    if usage is None:
                        continue
                    timestamp = snapshot_timestamp(record.get("timestamp"))
                    if timestamp is None:
                        return [], f"Supported token record lacks a timezone-aware ISO 8601 timestamp at {transcript.destination}:{line_number}."
                    snapshots.append(TokenSnapshot(timestamp, file_index, line_number, session, usage))
            if file_identity(before) != file_identity(os.fstat(descriptor)):
                raise RecordError(f"staged transcript changed while token usage was read: {transcript.destination}")
        finally:
            os.close(descriptor)
    return snapshots, ""


def extract_token_report(staging: Path, copies: tuple[SourceCopy, ...]) -> TokenReport:
    transcripts = tuple(copy for copy in copies if copy.role == "transcript")
    snapshots, unavailable_reason = token_snapshots(staging, transcripts)
    if unavailable_reason:
        return TokenReport(None, 0, unavailable_reason)
    if not snapshots:
        return TokenReport(None, 0, "No supported Codex total_token_usage records were present in the supplied transcripts.")
    previous: dict[str, TokenUsage] = {}
    total = TokenUsage(0, 0, 0, 0)
    for snapshot in sorted(snapshots, key=lambda item: (item.timestamp, item.file_index, item.line_number)):
        prior = previous.get(snapshot.session)
        total = total.add(snapshot.usage if prior is None else snapshot.usage.delta_from(prior))
        previous[snapshot.session] = snapshot.usage
    return TokenReport(total, len(snapshots))


def write_new_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RecordError(f"cannot create staged artifact: {path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_source(staging: Path, spec: SourceSpec) -> SourceCopy:
    destination = staging / spec.destination
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise RecordError(f"cannot create staged copy: {spec.destination}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        source_state = stream_file_state(spec.source, f"{spec.role} source", descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    destination_state = stream_file_state(destination, "staged copy")
    if destination_state != source_state:
        raise RecordError(f"staged copy differs from source: {spec.destination}")
    return SourceCopy(spec.role, spec.source, spec.destination, source_state)


def prepare_directories(staging: Path, specs: tuple[SourceSpec, ...]) -> None:
    directories: set[Path] = set()
    for spec in specs:
        directories.update(parent for parent in spec.destination.parents if parent != Path("."))
    for relative in sorted(directories, key=lambda path: (len(path.parts), path.parts)):
        directory = staging / relative
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)


def manifest_file_state(path: Path, relative_path: Path) -> dict[str, object]:
    state = stream_file_state(path, "staged artifact")
    return {"path": relative_path.as_posix(), "sha256": state.sha256, "size_bytes": state.size_bytes}


def token_manifest(report: TokenReport) -> dict[str, object]:
    if report.usage is None:
        return {
            "status": "unavailable",
            "reason": report.unavailable_reason,
            "supported_source": TOKEN_SOURCE,
            "timestamp_requirement": "Each counted record must have a timezone-aware ISO 8601 timestamp.",
        }
    usage = report.usage
    return {
        "status": "available",
        "supported_source": TOKEN_SOURCE,
        "timestamp_requirement": "Each counted record has a timezone-aware ISO 8601 timestamp.",
        "aggregation": TOKEN_AGGREGATION,
        "records_found": report.records_found,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }


def build_manifest(staging: Path, args: Args, elapsed: float, report: TokenReport, copies: tuple[SourceCopy, ...]) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for copy in copies:
        files.append(
            {
                "role": copy.role,
                "source": {"path": str(copy.source), "sha256": copy.source_state.sha256, "size_bytes": copy.source_state.size_bytes},
                "destination": manifest_file_state(staging / copy.destination, copy.destination),
            }
        )
    return {
        "schema": SCHEMA,
        "scope": {
            "statement": SCOPE_STATEMENT,
            "transcripts_supplied": len(args.transcripts),
            "prompt_files_supplied": 1,
            "input_attachments_supplied": len(args.inputs),
        },
        "timing": {"started_at": args.started_at, "ended_at": args.ended_at, "elapsed_seconds": elapsed},
        "token_usage": token_manifest(report),
        "files": files,
    }


def elapsed_text(elapsed: float) -> str:
    return f"{elapsed:.6f}".rstrip("0").rstrip(".")


def summary_text(args: Args, elapsed: float, report: TokenReport) -> str:
    lines = [
        "OMO experiment record",
        f"schema: {SCHEMA}",
        f"scope: {SCOPE_STATEMENT}",
        f"started at (caller supplied): {args.started_at}",
        f"ended at (caller supplied): {args.ended_at}",
        f"elapsed seconds: {elapsed_text(elapsed)}",
        f"transcript files preserved verbatim: {len(args.transcripts)}",
        "prompt files preserved as detached exact copies: 1",
        f"input files preserved as detached exact copies: {len(args.inputs)}",
    ]
    if report.usage is None:
        lines.append(f"token usage: unavailable; {report.unavailable_reason}")
    else:
        usage = report.usage
        lines.extend(
            (
                f"token usage: available from {report.records_found} supported cumulative record(s)",
                f"input tokens: {usage.input_tokens}",
                f"cached input tokens: {usage.cached_input_tokens}",
                f"output tokens: {usage.output_tokens}",
                f"reasoning output tokens: {usage.reasoning_output_tokens}",
                f"total tokens (input + output): {usage.input_tokens + usage.output_tokens}",
            )
        )
    return "\n".join(lines) + "\n"


def validate_staged_record(
    staging: Path,
    args: Args,
    elapsed: float,
    report: TokenReport,
    copies: tuple[SourceCopy, ...],
    manifest: dict[str, object],
    summary: str,
) -> None:
    for copy in copies:
        if stream_file_state(copy.source, f"{copy.role} source") != copy.source_state:
            raise RecordError(f"source changed before publication: {copy.source}")
        if stream_file_state(staging / copy.destination, "staged copy") != copy.source_state:
            raise RecordError(f"staged copy differs from source: {copy.destination}")
    if build_manifest(staging, args, elapsed, report, copies) != manifest:
        raise RecordError("staged artifact state differs from the manifest")
    manifest_payload = read_generated_bytes(staging / "manifest.json", "staged manifest")
    try:
        parsed_manifest = cast(object, json.loads(manifest_payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordError("staged manifest is not valid JSON") from exc
    if parsed_manifest != manifest or manifest.get("schema") != SCHEMA:
        raise RecordError("staged manifest does not match the validated record")
    summary_payload = read_generated_bytes(staging / "summary.txt", "staged summary")
    if summary_payload != summary.encode("utf-8") or SCOPE_STATEMENT not in summary:
        raise RecordError("staged summary does not contain the required record scope")
    expected_files = {copy.destination for copy in copies} | {Path("manifest.json"), Path("summary.txt")}
    actual_files: set[Path] = set()
    for path in staging.rglob("*"):
        if path.is_symlink():
            raise RecordError(f"staged record contains a symlink: {path.relative_to(staging)}")
        if path.is_file():
            relative = path.relative_to(staging)
            actual_files.add(relative)
            if path.stat().st_mode & 0o111:
                raise RecordError(f"staged artifact is executable: {relative}")
    if actual_files != expected_files:
        raise RecordError("staged record contains unexpected or missing files")


def open_directory_descriptor(path: Path, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecordError(f"cannot open {label}: {path}") from exc
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise RecordError(f"{label} is not a directory: {path}")


def fsync_directory_descriptor(descriptor: int, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise RecordError(f"cannot fsync {label}") from exc


def fsync_record_tree(staging: Path, copies: tuple[SourceCopy, ...]) -> None:
    directories = {staging, *(staging / parent for copy in copies for parent in copy.destination.parents if parent != Path("."))}
    for directory in sorted(directories, key=lambda path: (-len(path.parts), path.parts)):
        descriptor = open_directory_descriptor(directory, "staged record directory")
        try:
            fsync_directory_descriptor(descriptor, f"staged record directory {directory.relative_to(staging)}")
        finally:
            os.close(descriptor)


def rename_no_replace(parent_descriptor: int, source_name: str, destination_name: str) -> int:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise RecordError("this platform lacks atomic no-replace directory publication") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = cast(
        int,
        renameat2(
            parent_descriptor,
            os.fsencode(source_name),
            parent_descriptor,
            os.fsencode(destination_name),
            RENAME_NOREPLACE,
        ),
    )
    return ctypes.get_errno() if result != 0 else 0


def rollback_publication(staging_name: str, target: OutputTarget, original_error: BaseException) -> None:
    rollback_error = rename_no_replace(target.parent_descriptor, target.output.name, staging_name)
    if rollback_error:
        raise RecordError(f"publication validation failed and rollback failed: {os.strerror(rollback_error)}") from original_error
    try:
        fsync_directory_descriptor(target.parent_descriptor, "output parent directory after rollback")
    except RecordError as exc:
        raise RecordError("publication was rolled back but the output parent could not be synced") from exc


def publish_no_replace(staging_name: str, target: OutputTarget) -> None:
    revalidate_output_parent(target)
    error_number = rename_no_replace(target.parent_descriptor, staging_name, target.output.name)
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RecordError(f"output directory already exists: {target.output}")
    if error_number in {errno.ENOSYS, errno.EINVAL}:
        raise RecordError("this filesystem lacks atomic no-replace directory publication")
    if error_number:
        raise RecordError(f"cannot atomically publish record: {os.strerror(error_number)}")
    try:
        revalidate_output_parent(target)
        fsync_directory_descriptor(target.parent_descriptor, "output parent directory")
    except (OSError, RecordError) as exc:
        rollback_publication(staging_name, target, exc)
        raise


def create_record(args: Args) -> Path:
    target = open_output_target(args.output_dir)
    try:
        elapsed = elapsed_seconds(args)
        specs = source_specs(args)
        with tempfile.TemporaryDirectory(prefix=f".{target.output.name}.staging-", dir=target.parent_access_path) as temporary:
            staging = Path(temporary)
            prepare_directories(staging, specs)
            copies = tuple(copy_source(staging, spec) for spec in specs)
            report = extract_token_report(staging, copies)
            manifest = build_manifest(staging, args, elapsed, report, copies)
            summary = summary_text(args, elapsed, report)
            write_new_file(staging / "summary.txt", summary.encode("utf-8"))
            write_new_file(staging / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            validate_staged_record(staging, args, elapsed, report, copies, manifest, summary)
            fsync_record_tree(staging, copies)
            publish_no_replace(staging.name, target)
        return target.output
    finally:
        os.close(target.parent_descriptor)


def main(argv: list[str] | None = None) -> int:
    try:
        output = create_record(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, RecordError) as exc:
        print(f"omo_experiment_record: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
