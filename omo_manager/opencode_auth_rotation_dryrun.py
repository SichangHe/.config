#!/usr/bin/env python3
"""Safe inspector and human-gated Docker smoke for OpenCode auth rotation.

Default metadata inspection does not write files. Authorized smoke writes only
disposable container HOME files, an optional local Docker image, and optional
refreshed candidate credentials under a separate private helper output path.
It never mutates live OpenCode credential files or prints credential values.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import uuid
from dataclasses import dataclass
from os import getgid, getuid
from pathlib import Path
from typing import NoReturn, TypeAlias

DEFAULT_AUTH_DIR = Path.home() / ".local" / "share" / "opencode"
DEFAULT_REFRESHED_OUTPUT_DIR = Path.home() / ".local" / "share" / "opencode-auth-rotation" / "refreshed-smoke"
SENSITIVE_WORDS = (
    "access",
    "account",
    "api",
    "bearer",
    "cookie",
    "credential",
    "email",
    "id",
    "key",
    "refresh",
    "secret",
    "session",
    "token",
)
OPAQUE_KEY_RE = re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_-]{12,}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
JWTISH_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(\.[A-Za-z0-9_-]{10,})?$")
URLISH_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
SECRET_FIELD = r"authorization|cookie|credential|refresh(?:[_-]?token)?|access(?:[_-]?token)?|api[_-]?key|secret|session|token|key"
SECRET_VALUE_RE = re.compile(
    rf"(?i)([\"']?(?:{SECRET_FIELD})[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}}]+([\"']?)"
)
AUTH_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+")
BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
LONG_OPAQUE_RE = re.compile(r"\b(?=[A-Za-z0-9_./+=-]*[A-Za-z])(?=[A-Za-z0-9_./+=-]*[0-9])[A-Za-z0-9_./+=-]{32,}\b")
MAX_FAILURE_SNIPPET_CHARS = 2000
JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class CliArgs:
    auth_dir: Path
    current_name: str
    candidate_name: str
    max_depth: int
    smoke_mode: str
    human_authorized_smoke: bool
    docker_bin: str
    docker_image: str
    opencode_bin: Path
    prepare_smoke_image: str
    smoke_timeout_s: int
    smoke_command: str
    expected_output: str
    all_candidates: bool
    refreshed_output_dir: Path | None


@dataclass(frozen=True)
class NamedMatch:
    path: Path | None
    errors: tuple[str, ...]


class ParsedArgs(argparse.Namespace):
    auth_dir: Path = DEFAULT_AUTH_DIR
    current_name: str = "midas-team"
    candidate_name: str = ""
    max_depth: int = 3
    smoke_mode: str = "none"
    human_authorized_smoke: bool = False
    docker_bin: str = "docker"
    docker_image: str = "opencode-auth-smoke:local"
    opencode_bin: Path = Path.home() / ".opencode" / "bin" / "opencode"
    prepare_smoke_image: str = "run"
    smoke_timeout_s: int = 60
    smoke_command: str = "opencode run --pure --model openai/gpt-5.5 'Reply with exactly: yes'"
    expected_output: str = "yes"
    all_candidates: bool = False
    refreshed_output_dir: Path | None = None


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(
        description="No-op OpenCode auth rotation inspector. Prints no secrets."
    )
    _ = parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    _ = parser.add_argument("--current-name", default="midas-team")
    _ = parser.add_argument("--candidate-name", default="")
    _ = parser.add_argument("--max-depth", type=int, default=3)
    _ = parser.add_argument("--smoke-mode", choices=("none", "plan", "run"), default="none")
    _ = parser.add_argument("--human-authorized-smoke", action="store_true")
    _ = parser.add_argument("--docker-bin", default="docker")
    _ = parser.add_argument("--docker-image", default="opencode-auth-smoke:local")
    _ = parser.add_argument("--opencode-bin", type=Path, default=Path.home() / ".opencode" / "bin" / "opencode")
    _ = parser.add_argument("--prepare-smoke-image", choices=("none", "plan", "run"), default="run")
    _ = parser.add_argument("--smoke-timeout-s", type=int, default=60)
    _ = parser.add_argument(
        "--smoke-command",
        default="opencode run --pure --model openai/gpt-5.5 'Reply with exactly: yes'",
    )
    _ = parser.add_argument("--expected-output", default="yes")
    _ = parser.add_argument("--all-candidates", action="store_true")
    _ = parser.add_argument("--refreshed-output-dir", type=Path, default=None)
    args = parser.parse_args(argv, namespace=ParsedArgs())
    if args.max_depth < 1 or args.max_depth > 6:
        parser.error("--max-depth must be between 1 and 6.")
    if args.smoke_timeout_s < 1 or args.smoke_timeout_s > 600:
        parser.error("--smoke-timeout-s must be between 1 and 600.")
    for label, name in (
        ("--current-name", args.current_name),
        ("--candidate-name", args.candidate_name),
    ):
        if name and Path(name).name != name:
            parser.error(f"{label} must be a basename, not a path.")
    return CliArgs(
        auth_dir=args.auth_dir.expanduser(),
        current_name=args.current_name,
        candidate_name=args.candidate_name,
        max_depth=args.max_depth,
        smoke_mode=args.smoke_mode,
        human_authorized_smoke=args.human_authorized_smoke,
        docker_bin=args.docker_bin,
        docker_image=args.docker_image,
        opencode_bin=args.opencode_bin.expanduser(),
        prepare_smoke_image=args.prepare_smoke_image,
        smoke_timeout_s=args.smoke_timeout_s,
        smoke_command=args.smoke_command,
        expected_output=args.expected_output,
        all_candidates=args.all_candidates,
        refreshed_output_dir=args.refreshed_output_dir.expanduser() if args.refreshed_output_dir else None,
    )


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def redacted_key(key: str) -> str:
    lowered = key.lower()
    is_sensitive = (
        any(word in lowered for word in SENSITIVE_WORDS)
        or OPAQUE_KEY_RE.fullmatch(key) is not None
        or EMAIL_RE.fullmatch(key) is not None
        or JWTISH_RE.fullmatch(key) is not None
        or URLISH_RE.match(key) is not None
    )
    if is_sensitive:
        return "<redacted-key>"
    return key


def redact_failure_text(text: str) -> str:
    redacted = AUTH_BEARER_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    redacted = BEARER_RE.sub(lambda match: f"{match.group(1)}<redacted>", redacted)
    redacted = SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}<redacted>{match.group(2)}", redacted)
    redacted = OPENAI_KEY_RE.sub("<redacted-openai-key>", redacted)
    redacted = JWTISH_RE.sub("<redacted-token>", redacted)
    redacted = LONG_OPAQUE_RE.sub("<redacted-opaque>", redacted)
    return redacted


def failure_snippet(text: str) -> str:
    normalized = redact_failure_text(text).replace("\r", "")
    if len(normalized) <= MAX_FAILURE_SNIPPET_CHARS:
        return normalized
    return normalized[:MAX_FAILURE_SNIPPET_CHARS] + "\n<truncated>"


def print_block(label: str, text: str) -> None:
    print(f"{label}: |-")
    snippet = failure_snippet(text)
    if not snippet:
        print("  <empty>")
        return
    for line in snippet.splitlines():
        print(f"  {line}")


def file_mode(path: Path) -> str:
    return stat.filemode(path.stat().st_mode)


def is_regular_nonsymlink(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def as_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [as_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): as_json_value(child) for key, child in value.items()}
    return str(type(value).__name__)


def load_json(path: Path) -> JsonValue:
    with path.open("r", encoding="utf-8") as file:
        return as_json_value(json.load(file))


def shape(value: JsonValue, max_depth: int, depth: int = 0) -> JsonValue:
    if depth >= max_depth:
        return type(value).__name__
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        counts: dict[str, int] = {}
        for key in sorted(str(k) for k in value.keys()):
            child = value[key]
            safe_key = redacted_key(key)
            idx = counts.get(safe_key, 0)
            counts[safe_key] = idx + 1
            if idx:
                safe_key = f"{safe_key}#{idx + 1}"
            result[safe_key] = shape(child, max_depth, depth + 1)
        return result
    if isinstance(value, list):
        if not value:
            return []
        return [shape(value[0], max_depth, depth + 1), f"len={len(value)}"]
    return type(value).__name__


def find_named_file(auth_dir: Path, name: str) -> NamedMatch:
    if not name:
        return NamedMatch(path=None, errors=("candidate name is required",))
    candidates = [
        auth_dir / name,
        auth_dir / f"auth.{name}.json",
        auth_dir / f"auth-{name}.json",
        auth_dir / f"{name}.json",
    ]
    matches = tuple(candidate for candidate in candidates if candidate.exists())
    if len(matches) == 1:
        return NamedMatch(path=matches[0], errors=())
    if matches:
        names = ", ".join(path.name for path in matches)
        return NamedMatch(path=None, errors=(f"ambiguous auth name matches: {names}",))
    return NamedMatch(path=None, errors=("named auth file not found",))


def candidate_files(auth_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in auth_dir.glob("auth*.json")
        if is_regular_nonsymlink(path) and path.name != "auth.json"
    )


def file_readiness_errors(label: str, path: Path | None) -> list[str]:
    if path is None:
        return [f"{label} missing"]
    errors: list[str] = []
    try:
        lst = path.lstat()
    except OSError as exc:
        return [f"{label} stat failed: {type(exc).__name__}"]
    if stat.S_ISLNK(lst.st_mode):
        return [f"{label} must not be a symlink"]
    if not stat.S_ISREG(lst.st_mode):
        return [f"{label} must be a regular file"]
    if lst.st_uid != getuid():
        errors.append(f"{label} owner is not current user")
    st = path.stat()
    if st.st_mode & 0o077:
        errors.append(f"{label} permissions are too broad")
    try:
        _ = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} JSON unreadable: {type(exc).__name__}")
    return errors


def readiness_errors(
    auth_dir: Path,
    live_path: Path,
    current_match: NamedMatch,
    candidate_name: str,
    candidate_match: NamedMatch,
) -> list[str]:
    errors: list[str] = []
    try:
        dir_stat = auth_dir.lstat()
    except OSError as exc:
        errors.append(f"auth directory stat failed: {type(exc).__name__}")
    else:
        if stat.S_ISLNK(dir_stat.st_mode):
            errors.append("auth directory must not be a symlink")
        elif not stat.S_ISDIR(dir_stat.st_mode):
            errors.append("auth directory must be a directory")
        if dir_stat.st_uid != getuid():
            errors.append("auth directory owner is not current user")
        if dir_stat.st_mode & 0o077:
            errors.append("auth directory permissions are too broad")
    errors.extend(file_readiness_errors("live auth.json", live_path))
    errors.extend(f"current {error}" for error in current_match.errors)
    errors.extend(file_readiness_errors("current named auth file", current_match.path))
    if not candidate_name:
        errors.append("candidate name is required")
    else:
        errors.extend(f"candidate {error}" for error in candidate_match.errors)
        errors.extend(file_readiness_errors("candidate named auth file", candidate_match.path))
    if current_match.path is not None and current_match.path.name == "auth.json":
        errors.append("current named auth file must not be live auth.json")
    if candidate_match.path is not None and candidate_match.path.name == "auth.json":
        errors.append("candidate named auth file must not be live auth.json")
    if (
        current_match.path is not None
        and live_path.exists()
        and current_match.path.samefile(live_path)
    ):
        errors.append("current named auth file must not be live auth.json")
    if (
        candidate_match.path is not None
        and live_path.exists()
        and candidate_match.path.samefile(live_path)
    ):
        errors.append("candidate named auth file must not be live auth.json")
    if (
        current_match.path is not None
        and candidate_match.path is not None
        and current_match.path.samefile(candidate_match.path)
    ):
        errors.append("current and candidate named auth files must differ")
    return errors


def write_smoke_auth(candidate_path: Path, smoke_home: Path) -> None:
    auth_dir = smoke_home / ".local" / "share" / "opencode"
    auth_dir.mkdir(parents=True, mode=0o700)
    target = auth_dir / "auth.json"
    tmp_target = auth_dir / "auth.json.tmp"
    shutil.copyfile(candidate_path, tmp_target)
    tmp_target.chmod(0o600)
    tmp_target.replace(target)


def write_smoke_config(smoke_home: Path) -> None:
    config_dir = smoke_home / ".config" / "opencode"
    config_dir.mkdir(parents=True, mode=0o700)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"external_directory": "deny"},
        "plugin": [],
    }
    target = config_dir / "opencode.json"
    target.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target.chmod(0o600)


def opencode_bin_errors(path: Path) -> list[str]:
    try:
        lst = path.lstat()
    except OSError as exc:
        return [f"opencode binary stat failed: {type(exc).__name__}"]
    if stat.S_ISLNK(lst.st_mode):
        return ["opencode binary must not be a symlink"]
    if not stat.S_ISREG(lst.st_mode):
        return ["opencode binary must be a regular file"]
    if not (lst.st_mode & stat.S_IXUSR):
        return ["opencode binary must be executable by owner"]
    return []


def smoke_dockerfile() -> str:
    return textwrap.dedent(
        """
        FROM debian:bookworm-slim

        RUN apt-get update \\
          && apt-get install -y --no-install-recommends \\
            bash \\
            ca-certificates \\
            coreutils \\
            tini \\
          && rm -rf /var/lib/apt/lists/*

        RUN mkdir -p /workspace/bin /home/opencode-smoke
        ENV HOME=/home/opencode-smoke \\
            XDG_DATA_HOME=/home/opencode-smoke/.local/share \\
            OPENCODE_CONFIG_DIR=/home/opencode-smoke/.config/opencode \\
            PATH=/workspace/bin:/usr/local/bin:/usr/bin:/bin
        ENTRYPOINT ["/usr/bin/tini", "--"]
        CMD ["bash", "-lc", "opencode --version"]
        """
    ).lstrip()


def image_exists(args: CliArgs) -> bool:
    try:
        result = subprocess.run(
            [args.docker_bin, "image", "inspect", args.docker_image],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def prepare_smoke_image(args: CliArgs) -> int:
    if args.prepare_smoke_image == "none":
        print("smoke_image_prepare: skipped")
        return 0
    print(f"smoke_image: {args.docker_image}")
    print("smoke_image_pattern: VeruLaw-style minimal container plus passed-through opencode binary")
    if image_exists(args):
        print("smoke_image_prepare: already_present")
        return 0
    if args.prepare_smoke_image == "plan":
        print("smoke_image_prepare: planned")
        return 0
    with tempfile.TemporaryDirectory(prefix="opencode-auth-smoke-build-") as tmp:
        context = Path(tmp)
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(smoke_dockerfile(), encoding="utf-8")
        command = [
            args.docker_bin,
            "build",
            "--pull=false",
            "-f",
            str(dockerfile),
            "-t",
            args.docker_image,
            str(context),
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print("smoke_image_prepare: failed")
            print(f"smoke_image_error: {type(exc).__name__}")
            return 1
    if result.returncode == 0:
        print("smoke_image_prepare: built")
        return 0
    print("smoke_image_prepare: failed")
    print(f"smoke_image_exit_code: {result.returncode}")
    print_block("smoke_image_stdout_redacted", result.stdout)
    print_block("smoke_image_stderr_redacted", result.stderr)
    return 1


def docker_command(args: CliArgs, smoke_home: Path, container_name: str) -> list[str]:
    return [
        args.docker_bin,
        "run",
        "--rm",
        "--name",
        container_name,
        "--pull=never",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{getuid()}:{getgid()}",
        "--pids-limit=128",
        "--memory=512m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        "-e",
        "HOME=/home/opencode-smoke",
        "-e",
        "XDG_DATA_HOME=/home/opencode-smoke/.local/share",
        "-e",
        "OPENCODE_CONFIG_DIR=/home/opencode-smoke/.config/opencode",
        "-e",
        "PATH=/workspace/bin:/usr/local/bin:/usr/bin:/bin",
        "-v",
        f"{smoke_home}:/home/opencode-smoke",
        "-v",
        f"{args.opencode_bin}:/workspace/bin/opencode:ro",
        args.docker_image,
        "sh",
        "-c",
        args.smoke_command,
    ]


def print_smoke_plan(args: CliArgs, candidate_path: Path | None) -> None:
    print("docker_smoke_plan:")
    print(f"  mode: {args.smoke_mode}")
    candidate_name = candidate_path.name if candidate_path else "<not-ready>"
    print(f"  candidate_injected_as: {candidate_name} -> container auth.json")
    print(f"  docker_image: {args.docker_image}")
    print(f"  image_prepare: {args.prepare_smoke_image}")
    print("  image_pattern: self-prepared minimal image; host opencode binary is mounted read-only at /workspace/bin/opencode")
    print(f"  expected_output: {args.expected_output}")
    print("  prompt_contract: command must emit exactly expected_output after stripping whitespace")
    print("  cleanup: disposable temp HOME is deleted; docker runs with --rm")
    print("  timeout_cleanup: helper also runs docker rm -f -v on the generated container name")
    print("  logging: credential values are not printed; failure stdout/stderr are redacted and truncated")


def smoke_container_name() -> str:
    return f"opencode-auth-smoke-{uuid.uuid4().hex}"


def cleanup_container(args: CliArgs, container_name: str) -> None:
    command = [args.docker_bin, "rm", "-f", "-v", container_name]
    try:
        _ = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def refreshed_output_dir(args: CliArgs) -> Path:
    if args.refreshed_output_dir is not None:
        return args.refreshed_output_dir
    return DEFAULT_REFRESHED_OUTPUT_DIR


def write_refreshed_auth(args: CliArgs, candidate_path: Path, refreshed: bytes) -> Path:
    out_dir = refreshed_output_dir(args)
    out_dir.mkdir(parents=True, mode=0o700)
    out_dir.chmod(0o700)
    out_path = out_dir / f"{candidate_path.stem}.refreshed.{uuid.uuid4().hex}.json"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_bytes(refreshed)
    tmp_path.chmod(0o600)
    tmp_path.replace(out_path)
    return out_path


def run_smoke(args: CliArgs, candidate_path: Path) -> int:
    if not args.human_authorized_smoke:
        print("smoke_ready: false")
        print("smoke_error: --human-authorized-smoke is required for real candidate Docker smoke")
        return 2
    opencode_errors = opencode_bin_errors(args.opencode_bin)
    if opencode_errors:
        print("smoke_ready: false")
        for error in opencode_errors:
            print(f"smoke_error: {error}")
        return 2
    image_status = prepare_smoke_image(args)
    if image_status != 0:
        return image_status
    with tempfile.TemporaryDirectory(prefix="opencode-auth-smoke-") as tmp:
        smoke_home = Path(tmp)
        smoke_home.chmod(0o700)
        write_smoke_auth(candidate_path, smoke_home)
        write_smoke_config(smoke_home)
        smoke_auth_path = smoke_home / ".local" / "share" / "opencode" / "auth.json"
        before_auth = smoke_auth_path.read_bytes()
        container_name = smoke_container_name()
        command = docker_command(args, smoke_home, container_name)
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=args.smoke_timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print("smoke_matched_expected: false")
            print(f"smoke_error: {type(exc).__name__}")
            return 1
        finally:
            cleanup_container(args, container_name)
        after_auth = smoke_auth_path.read_bytes()
    observed = result.stdout.strip()
    matched = result.returncode == 0 and observed == args.expected_output
    print(f"smoke_exit_code: {result.returncode}")
    print(f"smoke_matched_expected: {str(matched).lower()}")
    if matched:
        print(f"smoke_output: {args.expected_output}")
    else:
        print(f"smoke_stdout_len: {len(result.stdout)}")
        print(f"smoke_stderr_len: {len(result.stderr)}")
        print_block("smoke_stdout_redacted", result.stdout)
        print_block("smoke_stderr_redacted", result.stderr)
    refreshed = after_auth != before_auth
    print(f"refreshed_credential_produced: {str(refreshed).lower()}")
    if refreshed:
        out_path = write_refreshed_auth(args, candidate_path, after_auth)
        print(f"refreshed_credential_path: {out_path}")
    return 0 if matched else 1


def print_all_candidate_plan(auth_dir: Path) -> None:
    print("all_candidate_smoke_plan:")
    files = candidate_files(auth_dir)
    print(f"  candidate_count: {len(files)}")
    for path in files:
        print(f"  candidate: {path.name}")
    print("  real_smoke_status: blocked_without_human_authorized_smoke")


def print_json_metadata(path: Path, max_depth: int) -> None:
    st = path.stat()
    print(f"file: {path.name}")
    print(f"  size_bytes: {st.st_size}")
    print(f"  mtime_unix: {int(st.st_mtime)}")
    print(f"  mode: {file_mode(path)}")
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  json: unreadable ({type(exc).__name__})")
        return
    if isinstance(data, dict):
        keys = ", ".join(redacted_key(str(key)) for key in sorted(data.keys()))
        print(f"  top_level_keys: {keys}")
    else:
        print(f"  top_level_type: {type(data).__name__}")
    print("  redacted_shape:")
    print(json.dumps(shape(data, max_depth), indent=4, sort_keys=True))


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    auth_dir = args.auth_dir.expanduser().absolute()
    print("OpenCode auth rotation helper; live credential files will not be changed.")
    print(f"auth_dir: {auth_dir}")
    if auth_dir.is_symlink():
        fail("auth directory must not be a symlink")
    if not auth_dir.is_dir():
        fail("auth directory does not exist")
    json_files = sorted(path for path in auth_dir.glob("*.json") if is_regular_nonsymlink(path))
    if not json_files:
        fail("no JSON files found in auth directory")
    live_path = auth_dir / "auth.json"
    current_match = find_named_file(auth_dir, args.current_name)
    candidate_match = find_named_file(auth_dir, args.candidate_name)
    print(f"live_auth_exists: {live_path.exists()}")
    current_name = current_match.path.name if current_match.path else "<not-ready>"
    candidate_name = candidate_match.path.name if candidate_match.path else "<not-ready>"
    print(f"current_named_match: {current_name}")
    print(f"candidate_named_match: {candidate_name}")
    errors = readiness_errors(
        auth_dir=auth_dir,
        live_path=live_path,
        current_match=current_match,
        candidate_name=args.candidate_name,
        candidate_match=candidate_match,
    )
    print(f"metadata_ready: {str(not errors).lower()}")
    print("rotation_ready: false")
    print("rotation_ready_reason: requires metadata_ready plus isolated smoke test and human-present authorization")
    for error in errors:
        print(f"readiness_error: {error}")
    print("rotation_plan_noop:")
    print("  preserve_current: copy live auth.json to current named file only after human authorizes")
    print("  test_candidate: use --smoke-mode plan or run for isolated Docker echo smoke")
    print("  live_switch: copy verified candidate to live auth.json only after human-present authorization")
    print("  rollback: restore pre-switch auth.json backup if smoke/monitoring fails")
    if args.smoke_mode != "none":
        print_smoke_plan(args, candidate_match.path)
    if args.all_candidates:
        print_all_candidate_plan(auth_dir)
    print("json_files:")
    for path in json_files:
        print_json_metadata(path, args.max_depth)
    if errors:
        return 2
    if args.smoke_mode == "run":
        if candidate_match.path is None:
            print("smoke_ready: false")
            print("smoke_error: candidate is not metadata-ready")
            return 2
        return run_smoke(args, candidate_match.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
