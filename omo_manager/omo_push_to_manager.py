#!/usr/bin/env python3
"""Push a reference-only notification into the OMO manager TUI."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MANAGER_URL = os.environ.get('OMO_MANAGER_URL', '')
DEFAULT_MANAGER_TARGET = os.environ.get('OMO_MANAGER_TMUX_TARGET', '')
DEFAULT_ROOT = Path(os.environ.get('OMO_WORK_LOGS_ROOT', Path.home() / 'work_logs'))


@dataclass(frozen=True)
class Args:
    text: str
    manager_url: str
    manager_target: str
    root: Path
    submit: bool
    timeout_s: float


class ParsedArgs(argparse.Namespace):
    text: str = ''
    manager_url: str = DEFAULT_MANAGER_URL
    manager_target: str = DEFAULT_MANAGER_TARGET
    root: Path = DEFAULT_ROOT
    submit: bool = False
    timeout_s: float = 5.0


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('text', help='Reference text, e.g. pending file-line ref.')
    _ = parser.add_argument('--manager-url', default=DEFAULT_MANAGER_URL)
    _ = parser.add_argument('--manager-target', default=DEFAULT_MANAGER_TARGET)
    _ = parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument('--submit', action='store_true', help='Submit the manager prompt after append.')
    _ = parser.add_argument('--timeout-s', type=float, default=5.0)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    text = parsed.text.strip()
    if not text:
        parser.error('text must not be empty.')
    manager_url = parsed.manager_url.strip().rstrip('/')
    manager_target = parsed.manager_target.strip()
    if not manager_url and not manager_target:
        parser.error('--manager-target or --manager-url is required.')
    return Args(text, manager_url, manager_target, parsed.root, parsed.submit, parsed.timeout_s)


def post_json(url: str, payload: object, timeout_s: float) -> None:
    data = json.dumps(payload).encode()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname is None:
        raise ValueError(f'unsupported manager URL: {url}')
    path = parsed.path or '/'
    if parsed.query:
        path = f'{path}?{parsed.query}'
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout_s)
    try:
        conn.request('POST', path, body=data, headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        body = resp.read().decode('utf-8', errors='replace').strip()
        if resp.status >= 400:
            raise RuntimeError(f'HTTP {resp.status} from {url}: {body}')
    finally:
        conn.close()
    if body not in {'true', ''}:
        raise RuntimeError(f'unexpected response from {url}: {body}')


def manager_endpoint(manager_url: str, route: str, root: Path) -> str:
    query = urllib.parse.urlencode({'directory': str(root)})
    return f'{manager_url}{route}?{query}'


def push_tmux(args: Args) -> None:
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', prefix='omo-manager-push.', delete=False) as handle:
        _ = handle.write(args.text)
        path = Path(handle.name)
    try:
        command = ['omo_tmux_send.py', '--target', args.manager_target, '--message-file', str(path)]
        if args.submit:
            command.append('--enter')
        _ = subprocess.run(command, timeout=args.timeout_s, check=True)
    finally:
        path.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.manager_target:
            push_tmux(args)
            return 0
        post_json(manager_endpoint(args.manager_url, '/tui/append-prompt', args.root), {'text': args.text}, args.timeout_s)
        if args.submit:
            post_json(manager_endpoint(args.manager_url, '/tui/submit-prompt', args.root), {}, args.timeout_s)
    except Exception as exc:
        print(f'omo_push_to_manager: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
