#!/usr/bin/env python3
"""Launch one configured Codex worker for a ready AMH Human-email route."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

AMH_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
AMH_SUBJECT_TAG_RE = re.compile(r"^\s*(?:re:\s*)*\[([A-Za-z0-9._-]{1,128})\](?:\s+|$)", re.IGNORECASE)
MAIN_MANAGER_AGENT_ID = "main-manager"
MAIN_MANAGER_SUBJECT_TAG = "main"
DEFAULT_MODEL = os.environ.get("OMO_AMH_CODEX_MODEL", "gpt-5.6-terra")
DEFAULT_REASONING_EFFORT = os.environ.get("OMO_AMH_CODEX_REASONING_EFFORT", "low")
DEFAULT_TMUX_SESSION = os.environ.get("OMO_AMH_TMUX_SESSION", "amh")
DEFAULT_WORKDIR = Path(os.environ.get("OMO_AMH_WORKDIR", "/ssd1/sichangheagent/amh"))


@dataclass(frozen=True, slots=True)
class RouteStatus:
    route_id: str
    operation_id: str
    source_id: str
    request_id: str
    destination_agent_id: str
    provider: str
    account_id: str
    sender_identity: str
    provider_message_id: str
    provider_thread_id: str
    exact_subject: str
    exact_payload: bytes
    payload_sha256: str


def route_id_from_operation(operation_id: str) -> str:
    if not operation_id or "\x00" in operation_id or "\n" in operation_id:
        raise ValueError("operation id must be nonempty and single-line")
    return "human-route-" + hashlib.sha256(operation_id.encode()).hexdigest()


def safe_task_suffix(route_id: str) -> str:
    return hashlib.sha256(route_id.encode()).hexdigest()[:12]


def load_route_status(amh_executable: Path, runtime_root: Path, route_id: str) -> RouteStatus:
    result = subprocess.run(
        [
            os.fspath(amh_executable),
            "--runtime-root",
            os.fspath(runtime_root),
            "task",
            "human-route-status",
            "--route",
            route_id,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AMH route status failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        root = json.loads(result.stdout)
        route = root["route"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("AMH route status did not return expected JSON") from exc
    if route.get("route_id") != route_id:
        raise RuntimeError("AMH route status returned a different route id")
    if route.get("route_kind") != "human_email":
        raise RuntimeError("AMH launcher only accepts Human-email routes")
    if route.get("state") != "ready":
        raise RuntimeError("AMH route is not ready for worker launch")
    destination = require_string(route, "destination_agent_id")
    if AMH_AGENT_ID_RE.fullmatch(destination) is None:
        raise RuntimeError("AMH route destination is not a valid agent id")
    operation_id = require_nonempty_string(route, "operation_id")
    source_id = require_nonempty_string(route, "source_id")
    request_id = require_nonempty_string(route, "request_id")
    metadata = require_object(route, "source_metadata")
    provider = require_nonempty_string(metadata, "provider")
    if provider != "gmail":
        raise RuntimeError("AMH route provider must be gmail for direct Human email launch")
    account_id = require_nonempty_string(metadata, "account_id")
    sender_identity = require_nonempty_string(metadata, "sender_identity")
    provider_message_id = require_nonempty_string(metadata, "provider_message_id")
    provider_thread_id = require_nonempty_string(metadata, "provider_thread_id")
    exact_subject = require_nonempty_string(metadata, "exact_subject")
    if invalid_email_title(f"Re: {exact_subject}"):
        raise RuntimeError("AMH route exact subject must produce one non-control-character line for email_me.py")
    subject_agent = subject_route_agent(exact_subject)
    if subject_agent != destination:
        raise RuntimeError("AMH route exact subject tag must match the destination agent")
    payload_sha256 = require_string(route, "payload_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None:
        raise RuntimeError("AMH route payload SHA-256 is invalid")
    try:
        exact_payload = base64.b64decode(require_string(route, "exact_payload"), validate=True)
    except ValueError as exc:
        raise RuntimeError("AMH route exact payload is not valid base64") from exc
    if hashlib.sha256(exact_payload).hexdigest() != payload_sha256:
        raise RuntimeError("AMH route exact payload digest mismatch")
    return RouteStatus(
        route_id=route_id,
        operation_id=operation_id,
        source_id=source_id,
        request_id=request_id,
        destination_agent_id=destination,
        provider=provider,
        account_id=account_id,
        sender_identity=sender_identity,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        exact_subject=exact_subject,
        exact_payload=exact_payload,
        payload_sha256=payload_sha256,
    )


def require_string(value: object, key: str) -> str:
    if not isinstance(value, dict):
        raise RuntimeError("AMH route is not an object")
    result = value.get(key)
    if not isinstance(result, str) or "\x00" in result:
        raise RuntimeError(f"AMH route field {key} is invalid")
    return result


def require_object(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("AMH route is not an object")
    result = value.get(key)
    if not isinstance(result, dict):
        raise RuntimeError(f"AMH route field {key} is required")
    return result


def require_nonempty_string(value: object, key: str) -> str:
    result = require_string(value, key)
    if not result:
        raise RuntimeError(f"AMH route field {key} is required")
    return result


def sentinel_safe_json_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def invalid_email_title(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or value.splitlines() != [value]


def subject_route_agent(subject: str) -> str:
    match = AMH_SUBJECT_TAG_RE.match(subject)
    if match is None:
        return ""
    remainder = subject[match.end() :]
    if re.match(r"^\s*\[[^\]]+\]", remainder) is not None:
        return ""
    agent_id = match.group(1)
    if agent_id.casefold() == MAIN_MANAGER_SUBJECT_TAG:
        return MAIN_MANAGER_AGENT_ID
    return agent_id


def write_prompt(path: Path, route: RouteStatus) -> None:
    exact_payload_base64 = base64.b64encode(route.exact_payload).decode()
    try:
        prompt_text = route.exact_payload.decode("utf-8")
        request_block = (
            "The Human request is valid UTF-8. The authoritative bytes are the base64 payload below.\n"
            "UTF-8 preview JSON string, escaped so it cannot be mistaken for manager control markup:\n"
            f"{sentinel_safe_json_text(prompt_text)}"
        )
    except UnicodeDecodeError:
        request_block = "The Human request is not valid UTF-8. Use the authoritative base64 payload below."
    reply_subject = sentinel_safe_json_text(f"Re: {route.exact_subject}")
    text = "".join(
        [
            "You are an AMH-owned Codex worker for one explicitly AMH-routed Human email.\n\nAMH route id: ",
            route.route_id,
            "\nAMH request id: ",
            route.request_id,
            "\nAMH source id: ",
            route.source_id,
            "\nAMH responsible agent id: ",
            route.destination_agent_id,
            "\nAMH provider: ",
            sentinel_safe_json_text(route.provider),
            "\nAMH provider account id: ",
            sentinel_safe_json_text(route.account_id),
            "\nAMH provider message id: ",
            sentinel_safe_json_text(route.provider_message_id),
            "\nAMH provider thread id: ",
            sentinel_safe_json_text(route.provider_thread_id),
            "\nAMH original Human sender: ",
            sentinel_safe_json_text(route.sender_identity),
            "\nAMH original email subject JSON string: ",
            sentinel_safe_json_text(route.exact_subject),
            "\nAMH exact payload byte length: ",
            str(len(route.exact_payload)),
            "\nAMH exact payload sha256: ",
            route.payload_sha256,
            "\nAMH exact payload base64: ",
            exact_payload_base64,
            "\n\nHandle exactly the Human request represented by those exact payload bytes. When you need to reply to the Human, use the current direct email practice: write a one-line subject file containing exactly ",
            reply_subject,
            ", write a message file, then run `/home/sichangheagent/.config/helper.sh/email_me.py --subject-file SUBJECT_FILE --message-file MESSAGE_FILE`. Do not ask the current manager or root to proxy your Human reply. Report to the manager with `omo_report.sh` only for lifecycle/status bookkeeping that should not go to the Human. Do not claim work outside this request.\n\n",
            request_block,
            "\n",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def receipt_path(state_dir: Path, route_id: str) -> Path:
    return state_dir / "amh-route-launches" / route_id / "launch-receipt.json"


def launch_lock_path(state_dir: Path, route_id: str) -> Path:
    return state_dir / "amh-route-launches" / route_id / "launch.lock"


def tmux_session_exists(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}:"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def prompt_path(state_dir: Path, route_id: str) -> Path:
    return state_dir / "amh-route-launches" / route_id / "prompt.md"


def completed_receipt_is_valid(path: Path, state_dir: Path, root: Path, route_id: str, operation_id: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected_task_file = f"amh_{safe_task_suffix(route_id)}.md"
    prompt = prompt_path(state_dir, route_id)
    if not prompt.is_file() or not (root / expected_task_file).is_file():
        return False
    prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
    return (
        isinstance(payload, dict)
        and payload.get("schema") == "omo-amh-route-launch/v1"
        and payload.get("status") == "launched"
        and payload.get("route_id") == route_id
        and payload.get("operation_id") == operation_id
        and isinstance(payload.get("destination_agent_id"), str)
        and AMH_AGENT_ID_RE.fullmatch(payload.get("destination_agent_id") or "") is not None
        and payload.get("provider") == "gmail"
        and isinstance(payload.get("account_id"), str)
        and bool(payload.get("account_id"))
        and isinstance(payload.get("sender_identity"), str)
        and bool(payload.get("sender_identity"))
        and isinstance(payload.get("provider_message_id"), str)
        and bool(payload.get("provider_message_id"))
        and isinstance(payload.get("provider_thread_id"), str)
        and bool(payload.get("provider_thread_id"))
        and isinstance(payload.get("exact_subject"), str)
        and bool(payload.get("exact_subject"))
        and payload.get("task_file") == expected_task_file
        and payload.get("prompt_sha256") == prompt_sha256
    )


def launch_route(args: argparse.Namespace) -> int:
    route_id = route_id_from_operation(args.operation_id)
    receipt = receipt_path(args.state_dir, route_id)
    if receipt.exists():
        if not completed_receipt_is_valid(receipt, args.state_dir, args.root, route_id, args.operation_id):
            raise RuntimeError("AMH route launch receipt is invalid or incomplete")
        print(receipt)
        return 0
    lock = launch_lock_path(args.state_dir, route_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("AMH route launch is already in progress or needs manual recovery") from exc
    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock_file:
        lock_file.write(f"route_id={route_id}\noperation_id={args.operation_id}\n")
    omo_task_attempted = False
    try:
        if receipt.exists():
            if not completed_receipt_is_valid(receipt, args.state_dir, args.root, route_id, args.operation_id):
                raise RuntimeError("AMH route launch receipt is invalid or incomplete")
            print(receipt)
            return 0
        route = load_route_status(args.amh_executable, args.amh_runtime_root, route_id)
        if route.operation_id != args.operation_id:
            raise RuntimeError("AMH route operation id differs from committed ingress operation")
        if not args.workdir.is_dir():
            raise RuntimeError("AMH workdir must be an existing configured directory")
        if not tmux_session_exists(args.tmux_session):
            raise RuntimeError("AMH tmux session must already exist")
        prompt = prompt_path(args.state_dir, route_id)
        write_prompt(prompt, route)
        task_file = f"amh_{safe_task_suffix(route_id)}.md"
        window_name = Path(task_file).stem
        command = [
            sys.executable,
            os.fspath(Path(__file__).with_name("omo_task.py")),
            "--root",
            os.fspath(args.root),
            "--task-file",
            task_file,
            "--tool",
            "codex",
            "--tmux-session",
            args.tmux_session,
            "--window-name",
            window_name,
            "--workdir",
            os.fspath(args.workdir),
            "--prompt-file",
            os.fspath(prompt),
            "--model",
            args.model,
            "--reasoning-effort",
            args.reasoning_effort,
            "--manager-target",
            args.manager_target,
            "--amh-caller-agent",
            route.destination_agent_id,
            "--require-existing-tmux-session",
        ]
        if args.dry_run:
            command.append("--dry-run")
        omo_task_attempted = True
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=args.launch_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"omo_task launch failed: {result.stderr.strip() or result.stdout.strip()}")
        if args.dry_run:
            print(prompt)
            return 0
        payload = {
            "schema": "omo-amh-route-launch/v1",
            "status": "launched",
            "route_id": route.route_id,
            "operation_id": route.operation_id,
            "source_id": route.source_id,
            "request_id": route.request_id,
            "destination_agent_id": route.destination_agent_id,
            "provider": route.provider,
            "account_id": route.account_id,
            "sender_identity": route.sender_identity,
            "provider_message_id": route.provider_message_id,
            "provider_thread_id": route.provider_thread_id,
            "exact_subject": route.exact_subject,
            "task_file": task_file,
            "tmux_session": args.tmux_session,
            "workdir": os.fspath(args.workdir),
            "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
            "omo_task_stdout": result.stdout,
        }
        fd, tmp_name = tempfile.mkstemp(prefix="launch-receipt-", suffix=".json", dir=receipt.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            os.replace(tmp_name, receipt)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        print(receipt)
        return 0
    finally:
        if receipt.exists() and completed_receipt_is_valid(receipt, args.state_dir, args.root, route_id, args.operation_id):
            lock.unlink(missing_ok=True)
        elif args.dry_run or not omo_task_attempted:
            lock.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("--root", type=Path, required=True)
    arg_parser.add_argument("--state-dir", type=Path, required=True)
    arg_parser.add_argument("--amh-executable", type=Path, required=True)
    arg_parser.add_argument("--amh-runtime-root", type=Path, required=True)
    arg_parser.add_argument("--operation-id", required=True)
    arg_parser.add_argument("--manager-target", required=True)
    arg_parser.add_argument("--tmux-session", default=DEFAULT_TMUX_SESSION)
    arg_parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    arg_parser.add_argument("--model", default=DEFAULT_MODEL)
    arg_parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"), default=DEFAULT_REASONING_EFFORT)
    arg_parser.add_argument("--launch-timeout-seconds", type=float, default=90.0)
    arg_parser.add_argument("--dry-run", action="store_true")
    return arg_parser


def main(argv: list[str]) -> int:
    try:
        args = parser().parse_args(argv)
        if not (args.root.is_absolute() and args.state_dir.is_absolute() and args.amh_runtime_root.is_absolute() and args.amh_executable.is_absolute()):
            raise RuntimeError("root, state dir, AMH executable, and AMH runtime root must be absolute paths")
        if args.tmux_session.startswith("h") or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", args.tmux_session) is None:
            raise RuntimeError("AMH tmux session must be a non-human exact session name")
        if not args.manager_target:
            raise RuntimeError("manager target is required for omo_task launch bookkeeping")
        return launch_route(args)
    except Exception as exc:
        print(f"omo_amh_route_launch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
