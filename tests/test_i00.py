"""Behavioural tests for I00 tooling, without live desktop or VDI access."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("i00", ROOT / "tools/i00.py")
i00 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i00)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "artifacts/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.fixtures = self.path / "fixtures"
        with contextlib.redirect_stdout(io.StringIO()):
            i00.fixtures(argparse.Namespace(output=self.fixtures, size_kib=64))

    def test_fixture_golden_stream_is_not_random_version_dependent(self):
        first = hashlib.sha256(b"qr-transfer-i00-v1:binary:" + (0).to_bytes(8, "big")).digest()
        self.assertEqual((self.fixtures / "binary.bin").read_bytes()[:32], first)
        other = self.path / "other"
        with contextlib.redirect_stdout(io.StringIO()):
            i00.fixtures(argparse.Namespace(output=other, size_kib=64))
        self.assertEqual(i00.read_json(self.fixtures / "manifest.json"), i00.read_json(other / "manifest.json"))

    def test_existing_fixture_directory_is_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            i00.fixtures(argparse.Namespace(output=self.fixtures, size_kib=64))

    def test_generated_output_cannot_become_its_own_input(self):
        with self.assertRaises(ValueError):
            i00.prepare(argparse.Namespace(fixtures=self.fixtures, output=self.fixtures / "tree/output"))
        with self.assertRaises(ValueError):
            i00.browser_bundle(argparse.Namespace(output=ROOT / "diagnostics/browser/copy"))

    def test_modified_input_fails_before_preparation(self):
        (self.fixtures / "binary.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Changed fixture"):
            i00.verify_fixture_set(self.fixtures)

    def test_unicode_directory_archive_verified_without_extraction(self):
        archive_path = self.path / "tree.tar.xz"
        with tarfile.open(archive_path, "w:xz") as archive:
            archive.add(self.fixtures / "tree", arcname="tree")
        result = i00.verify_received(archive_path, self.fixtures, "tree")
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["directories"], 3)
        self.assertFalse(result["extracted"])
        self.assertFalse((self.path / "tree").exists())

    def test_unexpected_archive_member_rejected(self):
        archive_path = self.path / "bad.tar.xz"
        with tarfile.open(archive_path, "w:xz") as archive:
            archive.add(self.fixtures / "tree", arcname="tree")
            record = tarfile.TarInfo("../../unexpected")
            record.size = 1
            archive.addfile(record, io.BytesIO(b"x"))
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            i00.verify_received(archive_path, self.fixtures, "tree")

    def test_received_binary_hash_is_independently_checked(self):
        path = self.path / "received.bin"
        path.write_bytes((self.fixtures / "binary.bin").read_bytes()[:-1] + b"x")
        with self.assertRaises(ValueError):
            i00.verify_received(path, self.fixtures, "binary")

    def test_legacy_zero_exit_without_output_is_failure_with_report(self):
        output = self.path / "timeout"
        args = argparse.Namespace(fixtures=self.fixtures, output=output, case="binary",
                                  monitor=1, fps=12, timeout=1, level="local_screen")
        with patch.object(i00, "capture_probe", return_value={"status": "not_checked"}), \
             patch.object(i00, "run_process", return_value={"returncode": 0, "termination": None}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(i00.receive(args), 1)
        self.assertFalse(i00.read_json(output / "receive.json")["ok"])

    def test_report_never_overwrites_previous_measurement(self):
        path = self.path / "report.json"
        i00.write_json(path, {"run": 1})
        with self.assertRaises(FileExistsError):
            i00.write_json(path, {"run": 2})
        self.assertEqual(json.loads(path.read_text()), {"run": 1})

    def test_timeout_stops_real_child_and_records_failure(self):
        result = i00.run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                 self.path / "timeout.log", timeout=0.1)
        self.assertEqual(result["termination"], "timeout")
        self.assertNotEqual(result["returncode"], 0)
        self.assertLess(result["wall_seconds"], 10)

    def test_interrupted_receive_keeps_failure_report(self):
        output = self.path / "interrupted"
        args = argparse.Namespace(fixtures=self.fixtures, output=output, case="binary",
                                  monitor=1, fps=12, timeout=1, level="local_screen")
        with patch.object(i00, "capture_probe", return_value={"status": "not_checked"}), \
             patch.object(i00, "run_process", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(i00.receive(args), 130)
        report = i00.read_json(output / "receive.json")
        self.assertFalse(report["ok"])
        self.assertEqual(report["process"]["termination"], "interrupted")

    def test_invalid_capture_parameters_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            i00.nonnegative("-1")
        for value in ("0", "-2"):
            with self.assertRaises(argparse.ArgumentTypeError):
                i00.positive(value)

    def test_diagnostic_error_redacts_url_credentials(self):
        result = i00.error_info(ValueError("Failed https://user:password@mirror.invalid/pypi?token=secret"))
        self.assertNotIn("password", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
