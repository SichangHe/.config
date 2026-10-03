#!/usr/bin/env python3
"""Fail fast when a VL experiment launch lacks helper or verifier setup."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

OPENROUTER_NEEDLE = "openrouter"


@dataclass(frozen=True)
class Args:
    midas_lex: Path | None
    verus: Path | None
    artifact_root: Path | None
    require_staged_verus: bool
    require_gpt_backed: bool
    evidence_dir: Path | None


@dataclass(frozen=True)
class CheckResult:
    label: str
    details: str


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--midas-lex", type=Path, help="Intended `midas-lex` executable that must resolve from PATH.")
    _ = parser.add_argument("--verus", type=Path, help="Verifier executable. Defaults to $VERUS or `verus` on PATH.")
    _ = parser.add_argument("--artifact-root", type=Path, help="Experiment artifact root used to decide whether Verus is staged locally.")
    _ = parser.add_argument("--require-staged-verus", action="store_true", help="Fail unless the verifier executable is inside --artifact-root.")
    _ = parser.add_argument("--allow-openrouter", action="store_true", help="Skip GPT-backed OpenRouter absence checks.")
    _ = parser.add_argument("--evidence-dir", type=Path, help="Directory where `preflight.txt` will be written.")
    parsed = parser.parse_args(argv)
    verus = parsed.verus
    if verus is None and os.environ.get("VERUS"):
        verus = Path(os.environ["VERUS"])
    return Args(parsed.midas_lex, verus, parsed.artifact_root, parsed.require_staged_verus, not parsed.allow_openrouter, parsed.evidence_dir)


def run_cmd(
    args: list[str],
    timeout_s: float = 15.0,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, env=env, cwd=cwd)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def executable(path: Path) -> Path:
    if path.is_dir():
        path = path / "verus"
    resolved = path.resolve(strict=False)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"not executable: {resolved}")
    return resolved


def check_midas_lex(midas_lex: Path | None) -> CheckResult:
    found = shutil.which("midas-lex")
    if found is None:
        raise ValueError("`midas-lex` is not on PATH")
    found_entry = Path(found).absolute()
    found_path = found_entry.resolve(strict=False)
    if midas_lex is not None:
        intended = executable(midas_lex)
        if found_path != intended:
            raise ValueError(f"`midas-lex` resolves to {found_path}; expected {intended}")
    with tempfile.TemporaryDirectory(prefix="omo-midas-lex-help-") as scratch:
        scratch_path = Path(scratch)
        help_env = {
            "HOME": str(scratch_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "XDG_CONFIG_HOME": str(scratch_path / "xdg"),
            "MIDAS_LEX_RESOURCE_DIR": str(scratch_path / "resources"),
        }
        help_result = run_cmd([str(found_entry), "help"], env=help_env, cwd=scratch_path)
    if help_result.returncode != 0:
        raise ValueError(f"`midas-lex help` failed with exit {help_result.returncode}: {help_result.stderr.strip()}")
    return CheckResult(
        "midas-lex",
        f"path={found_path}\nhelp_exit=0\nhelp_state=fresh_scratch_removed",
    )


def find_verus(verus: Path | None) -> Path:
    if verus is not None:
        return executable(verus)
    found = shutil.which("verus")
    if found is None:
        raise ValueError("Verus executable not provided by --verus, $VERUS, or PATH")
    return executable(Path(found))


def version_text(verus: Path) -> str:
    for flag in ("--version", "-V"):
        result = run_cmd([str(verus), flag])
        text = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        if result.returncode == 0 and text:
            return text
    version_txt = verus.parent / "version.txt"
    if version_txt.is_file():
        return version_txt.read_text(encoding="utf-8").strip()
    version_json = verus.parent / "version.json"
    if version_json.is_file():
        return version_json.read_text(encoding="utf-8").strip()
    raise ValueError(f"could not get Verus version from {verus}")


def check_verus(verus_arg: Path | None, artifact_root: Path | None, require_staged: bool) -> CheckResult:
    verus = find_verus(verus_arg)
    if artifact_root is not None:
        root = artifact_root.resolve(strict=False)
        staged = verus == root or root in verus.parents
        if require_staged and not staged:
            raise ValueError(f"Verus is {verus}, outside artifact root {root}")
        staged_text = str(staged).lower()
    else:
        staged_text = "unknown"
    version = version_text(verus)
    return CheckResult("verus", f"path={verus}\nsha256={sha256(verus)}\nversion={version}\nstaged_in_artifact_root={staged_text}")


def check_openrouter_absent() -> CheckResult:
    hits = []
    for key, value in os.environ.items():
        if OPENROUTER_NEEDLE in key.lower() or OPENROUTER_NEEDLE in value.lower():
            hits.append(key)
    if hits:
        raise ValueError("OpenRouter env present for GPT-backed route: " + ", ".join(sorted(hits)))
    return CheckResult("openrouter", "absent=true")


def evidence(results: list[CheckResult]) -> str:
    blocks = []
    for result in results:
        blocks.append(f"[{result.label}]\n{result.details}")
    return "\n\n".join(blocks) + "\n"


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        results = [check_midas_lex(args.midas_lex), check_verus(args.verus, args.artifact_root, args.require_staged_verus)]
        if args.require_gpt_backed:
            results.append(check_openrouter_absent())
        text = evidence(results)
        if args.evidence_dir is not None:
            args.evidence_dir.mkdir(parents=True, exist_ok=True)
            _ = (args.evidence_dir / "preflight.txt").write_text(text, encoding="utf-8")
        print(text, end="")
    except Exception as exc:
        print(f"omo_vl_experiment_preflight: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
