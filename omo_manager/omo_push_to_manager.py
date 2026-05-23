#!/usr/bin/env python3
"""Push a reference-only notification into the OMO manager OpenCode TUI."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"

DEFAULT_MANAGER_URL = os.environ.get("OMO_MANAGER_URL", "")
DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))


@dataclass(frozen=True)
class Args:
    text: str
    manager_url: str
    root: Path
    submit: bool
    timeout_s: float


class ParsedArgs(argparse.Namespace):
    text: str = ""
    manager_url: str = DEFAULT_MANAGER_URL
    root: Path = DEFAULT_ROOT
    submit: bool = False
    timeout_s: float = 5.0


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("text", help="Reference text, e.g. `pending: file=x.md line=12`.")
    _ = parser.add_argument("--manager-url", default=DEFAULT_MANAGER_URL)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--submit", action="store_true", help="Submit the manager prompt after append.")
    _ = parser.add_argument("--timeout-s", type=float, default=5.0)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    text = parsed.text.strip()
    if not text:
        parser.error("`text` must not be empty.")
    manager_url = (parsed.manager_url.strip() or detect_manager_url()).rstrip("/")
    if not manager_url:
        parser.error("--manager-url is required unless OMO_MANAGER_URL or OPENCODE_PID identifies the manager TUI.")
    return Args(text=text, manager_url=manager_url, root=parsed.root, submit=parsed.submit, timeout_s=parsed.timeout_s)


def detect_manager_url() -> str:
    env_url = os.environ.get("OMO_MANAGER_URL", "").strip()
    if env_url:
        return env_url
    pid = os.environ.get("OPENCODE_PID", "").strip()
    if not pid:
        return ""
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    needle = f"pid={pid},"
    for line in out.splitlines():
        parts = line.split()
        if needle in line and len(parts) >= 4 and parts[3].startswith("127.0.0.1:"):
            return f"http://{parts[3]}"
    return ""


def post_json(url: str, payload: object, timeout_s: float) -> None:
    data = json.dumps(payload).encode("utf-8")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ValueError(f"unsupported manager URL: {url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout_s)
    try:
        conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace").strip()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from {url}: {body}")
    finally:
        conn.close()
    if body not in {"true", ""}:
        raise RuntimeError(f"unexpected response from {url}: {body}")


def manager_endpoint(manager_url: str, route: str, root: Path) -> str:
    query = urllib.parse.urlencode({"directory": str(root)})
    return f"{manager_url}{route}?{query}"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        post_json(manager_endpoint(args.manager_url, "/tui/append-prompt", args.root), {"text": args.text}, args.timeout_s)
    except Exception as exc:
        print(f"omo_push_to_manager: {exc}", file=sys.stderr)
        return 1
    if args.submit:
        try:
            post_json(manager_endpoint(args.manager_url, "/tui/submit-prompt", args.root), {}, args.timeout_s)
        except Exception as exc:
            print(f"omo_push_to_manager: appended but submit failed: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
