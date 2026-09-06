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
    loader = SourceFileLoader(
        "getagentsmd", str(Path(__file__).with_name("getagentsmd"))
    )
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
                module.main([])

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
            module.get = lambda url, timeout: (_ for _ in ()).throw(
                module.RequestException("network down")
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                module.main([])

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
                module.main([])

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

    def test_list_fetches_index_and_caches_it(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            module.CACHE_DIR = Path(tmp)
            (Path(tmp) / "index.md").write_text("stale\n", encoding="utf-8")
            (Path(tmp) / "index.md").chmod(0o600)
            requested = []
            module.get = lambda url, timeout: requested.append(url) or Response(
                200, "available\n"
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, module.main(["list"]))

            self.assertEqual([f"{module.INSTRUCTIONS_URL}/index.md"], requested)
            self.assertEqual("available\n", out.getvalue())
            self.assertEqual(
                "available\n", (Path(tmp) / "index.md").read_text(encoding="utf-8")
            )

    def test_get_fetches_only_explicit_instruction(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            module.CACHE_DIR = Path(tmp)
            (Path(tmp) / "coding.md").write_text("stale\n", encoding="utf-8")
            (Path(tmp) / "coding.md").chmod(0o600)
            requested = []
            module.get = lambda url, timeout: requested.append(url) or Response(
                200, "python choices are optional\n"
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, module.main(["get", "coding"]))

            self.assertEqual([f"{module.INSTRUCTIONS_URL}/coding.md"], requested)
            self.assertEqual("python choices are optional\n", out.getvalue())
            self.assertEqual(
                "python choices are optional\n",
                (Path(tmp) / "coding.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((Path(tmp) / "python.md").exists())

    def test_named_instruction_uses_its_cache_after_remote_failure(self):
        module = load_getagentsmd()
        with tempfile.TemporaryDirectory() as tmp:
            module.CACHE_DIR = Path(tmp)
            cache_file = Path(tmp) / "python.md"
            cache_file.write_text("cached python\n", encoding="utf-8")
            cache_file.chmod(0o600)
            module.get = lambda url, timeout: (_ for _ in ()).throw(
                module.RequestException("network down")
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, module.main(["get", "python"]))

            self.assertEqual(
                f"{module.FALLBACK_NOTICE}\ncached python\n", out.getvalue()
            )

    def test_get_rejects_paths_and_does_not_fetch(self):
        module = load_getagentsmd()
        module.get = lambda url, timeout: self.fail("invalid names must not be fetched")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, module.main(["get", "coding/python"]))

        self.assertEqual(
            "Usage: getagentsmd [list | get INSTRUCTION_NAME]\n", err.getvalue()
        )

    def test_new_commands_fail_without_remote_or_cache(self):
        for args in (["list"], ["get", "coding"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                module = load_getagentsmd()
                module.CACHE_DIR = Path(tmp)
                module.get = lambda url, timeout: (_ for _ in ()).throw(
                    module.RequestException("network down")
                )

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(1, module.main(args))

                self.assertIn("Failed to fetch agent instructions", out.getvalue())


if __name__ == "__main__":
    unittest.main()
