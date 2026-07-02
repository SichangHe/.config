#!/usr/bin/env python3
import contextlib
import io
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


def load_getagentsmd():
    loader = SourceFileLoader("getagentsmd", str(Path(__file__).with_name("getagentsmd")))
    spec = spec_from_loader("getagentsmd", loader)
    assert spec is not None
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class GetAgentsMdTest(unittest.TestCase):
    def test_success_prints_rendered_output_and_caches_it(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            module.CACHE_DIR = Path(tmp)
            module.CACHE_FILE = Path(tmp) / "AGENTS.md"
            module.load_env_var = lambda name: "/notes" if name == "NOTES_DIR" else None
            module.get = lambda url, timeout: Response(200, "remote\n")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                module.main()

            expected = "remote\n\nThe human's notes are at /notes, for you reference\n"
            self.assertEqual(expected, out.getvalue())
            self.assertEqual(expected, module.CACHE_FILE.read_text(encoding="utf-8"))

    def test_failed_fetch_uses_cache_with_report_instruction(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            module.CACHE_DIR = Path(tmp)
            module.CACHE_FILE = Path(tmp) / "AGENTS.md"
            module.CACHE_FILE.write_text("cached\n", encoding="utf-8")
            module.CACHE_FILE.chmod(0o600)
            module.get = lambda url, timeout: (_ for _ in ()).throw(module.RequestException("network down"))

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                module.main()

            self.assertEqual(f"{module.FALLBACK_NOTICE}\ncached\n", out.getvalue())

    def test_http_error_uses_cache_with_report_instruction(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            module.CACHE_DIR = Path(tmp)
            module.CACHE_FILE = Path(tmp) / "AGENTS.md"
            module.CACHE_FILE.write_text("cached\n", encoding="utf-8")
            module.CACHE_FILE.chmod(0o600)
            module.get = lambda url, timeout: Response(500)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                module.main()

            self.assertEqual(f"{module.FALLBACK_NOTICE}\ncached\n", out.getvalue())

    def test_write_cache_rejects_symlink_cache_dir(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            link = root / "cache-link"
            target.mkdir()
            os.symlink(target, link)
            module.CACHE_DIR = link
            module.CACHE_FILE = link / "AGENTS.md"

            module.write_cache("cached\n")

            self.assertFalse((target / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
