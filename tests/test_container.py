from __future__ import annotations

from dataclasses import replace
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import getpass
import zlib

from qr_transfer import container as c
from qr_transfer.cli import main
from qr_transfer.transfer import prepare_object, packets, Receiver
from qr_transfer.stream import read_stream

PASSWORD = "only-a-test-password-123"


@unittest.skipUnless(importlib.util.find_spec("py7zr"), "Install .[container] for container tests")
class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.py7zr = c.backend()

    def source(self, data=b"\0\xff\x80test"):
        path = self.root / "source.bin"
        path.write_bytes(data)
        return path

    def prepare(self, source=None):
        archive = c.prepare(source or self.source(), self.root / "object.7z", PASSWORD)
        descriptor = prepare_object(archive, container="7z-aes256").descriptor
        return archive, descriptor

    def assert_clean(self):
        self.assertEqual(list(self.root.glob(".qr-transfer-*")), [])

    def hostile(self, entries, manifest=None, *, header=True, encrypt=True, attributes=None):
        archive = self.root / "hostile.7z"
        filters = [{"id": self.py7zr.FILTER_LZMA2, "preset": 1}]
        if encrypt:
            filters.append({"id": self.py7zr.FILTER_CRYPTO_AES256_SHA256})
        with self.py7zr.SevenZipFile(archive, "w", password=PASSWORD, header_encryption=header, filters=filters) as writer:
            writer.writestr(json.dumps(manifest or {"invalid": True}).encode(), c.MANIFEST)
            for index, (name, content) in enumerate(entries):
                writer.writestr(content, f"safe{index}")
                # Construct invalid headers without invoking filesystem traversal.
                writer.header.files_info.files[-1]["filename"] = name
                if attributes is not None:
                    writer.header.files_info.files[-1]["attributes"] = attributes
        return archive, prepare_object(archive, container="7z-aes256").descriptor

    def test_empty_small_random_and_compressed_roundtrip(self):
        cases = (b"", b"a", bytes(range(256)) * 128, zlib.compress(bytes(range(256)) * 200))
        for index, data in enumerate(cases):
            with self.subTest(size=len(data)):
                source = self.root / f"file{index}.bin"
                source.write_bytes(data)
                archive = c.prepare(source, self.root / f"object{index}.7z", PASSWORD)
                descriptor = prepare_object(archive, container="7z-aes256").descriptor
                result = c.extract(archive, self.root / f"out{index}", PASSWORD, descriptor=descriptor)
                self.assertEqual((result / source.name).read_bytes(), data)
                self.assert_clean()

    def test_tree_unicode_empty_dirs_and_no_hidden_exclusions(self):
        source = self.root / "данные"
        (source / "пусто").mkdir(parents=True)
        (source / ".git").mkdir()
        (source / ".git/config").write_bytes(b"synthetic")
        (source / "manifest.json").write_bytes(b"user-owned")
        (source / "😀.txt").write_bytes("Привет".encode())
        archive, descriptor = self.prepare(source)
        result = c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor) / source.name
        self.assertTrue((result / "пусто").is_dir())
        self.assertEqual((result / "manifest.json").read_bytes(), b"user-owned")
        self.assertEqual((result / ".git/config").read_bytes(), b"synthetic")
        self.assertEqual((result / "😀.txt").read_bytes(), "Привет".encode())

    def test_empty_directory_roundtrip(self):
        source = self.root / "empty"
        source.mkdir()
        archive, descriptor = self.prepare(source)
        result = c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)
        self.assertEqual(list((result / "empty").iterdir()), [])

    def test_names_hidden_data_encrypted_and_password_retry(self):
        archive, descriptor = self.prepare()
        with self.assertRaises(self.py7zr.exceptions.PasswordRequired):
            self.py7zr.SevenZipFile(archive, "r")
        with self.py7zr.SevenZipFile(archive, "r", password=PASSWORD) as reader:
            c._require_encrypted(reader)
            self.assertIn("data/source.bin", reader.getnames())
        with self.assertRaisesRegex(c.ContainerError, "password_or_damaged_container"):
            c.extract(archive, self.root / "out", "wrong", descriptor=descriptor)
        self.assertTrue(archive.exists())
        self.assertFalse((self.root / "out").exists())
        self.assert_clean()
        c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)

    def test_random_iv_and_public_metadata_no_source_names(self):
        source = self.source()
        first, descriptor = self.prepare(source)
        second = c.prepare(source, self.root / "second.7z", PASSWORD)
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertNotIn(source.name, descriptor.to_bytes().decode())
        self.assertNotIn(PASSWORD, descriptor.to_bytes().decode())

    def test_transport_corruption_before_prompt(self):
        archive, descriptor = self.prepare()
        path = self.root / "descriptor.json"
        path.write_bytes(descriptor.to_bytes())
        data = bytearray(archive.read_bytes()); data[-1] ^= 1; archive.write_bytes(data)
        with patch.object(c, "password_input") as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["unpack", str(archive), "--descriptor", str(path), "--output", str(self.root / "out")]), 1)
            prompt.assert_not_called()
        self.assertFalse((self.root / "out").exists())

    def test_corrupt_ciphertext_with_matching_transport_hash(self):
        archive, _ = self.prepare()
        raw = bytearray(archive.read_bytes()); raw[40] ^= 1; archive.write_bytes(raw)
        descriptor = prepare_object(archive, container="7z-aes256").descriptor
        with self.assertRaises(c.ContainerError):
            c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)
        self.assertFalse((self.root / "out").exists())
        self.assertTrue(archive.exists())
        self.assert_clean()

    def test_hidden_input_empty_mismatch_no_echo_fallback(self):
        with patch.object(getpass, "getpass", return_value=""):
            with self.assertRaisesRegex(c.ContainerError, "empty_password"):
                c.password_input()

    def test_accidental_password_argument_not_echoed(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(["pack", "x", "--output", "y", "--password", PASSWORD])
        self.assertNotIn(PASSWORD, stderr.getvalue())
        with patch.object(getpass, "getpass", side_effect=["first", "second"]):
            with self.assertRaisesRegex(c.ContainerError, "password_mismatch"):
                c.password_input(confirm=True)
        def warn(*_):
            warnings.warn("no tty", getpass.GetPassWarning)
        with patch.object(getpass, "getpass", side_effect=warn):
            with self.assertRaisesRegex(c.ContainerError, "hidden_password_input_unavailable"):
                c.password_input()

    def test_existing_destination_preserved(self):
        source = self.source()
        target = self.root / "object.7z"
        target.write_bytes(b"keep")
        with self.assertRaisesRegex(c.ContainerError, "destination_exists"):
            c.prepare(source, target, PASSWORD)
        self.assertEqual(target.read_bytes(), b"keep")
        target.unlink()
        archive, descriptor = self.prepare(source)
        out = self.root / "out"; out.mkdir(); (out / "keep").write_bytes(b"keep")
        with self.assertRaisesRegex(c.ContainerError, "destination_exists"):
            c.extract(archive, out, PASSWORD, descriptor=descriptor)
        self.assertEqual((out / "keep").read_bytes(), b"keep")

    def test_links_and_special_source_rejected(self):
        source = self.source()
        os.link(source, self.root / "linked.bin")
        with self.assertRaisesRegex(c.ContainerError, "hardlinks_not_supported"):
            self.prepare(source)
        with self.assertRaisesRegex(c.ContainerError, "special_files_not_supported"):
            c._plain(type("Stat", (), {"st_mode": stat.S_IFIFO, "st_nlink": 1})())
        with self.assertRaisesRegex(c.ContainerError, "links_not_supported"):
            c._plain(type("Stat", (), {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400})())

    def test_source_mutation_and_cancel_cleanup(self):
        source = self.source()
        original = self.py7zr.SevenZipFile.write
        def changed(writer, *args, **kwargs):
            result = original(writer, *args, **kwargs)
            source.write_bytes(b"changed")
            return result
        with patch.object(self.py7zr.SevenZipFile, "write", changed):
            with self.assertRaisesRegex(c.ContainerError, "source_changed"):
                self.prepare(source)
        self.assert_clean()
        self.assertFalse((self.root / "object.7z").exists())
        with patch.object(self.py7zr.SevenZipFile, "write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare(source)
        self.assert_clean()
        self.assertTrue(source.exists())

    def test_source_output_nesting_rejected(self):
        source = self.root / "tree"; source.mkdir()
        with self.assertRaisesRegex(c.ContainerError, "output_inside_source"):
            c.prepare(source, source / "object.7z", PASSWORD)

    def test_limits_and_disk_space(self):
        source = self.source(b"1234")
        with self.assertRaisesRegex(c.ContainerError, "expanded_size_limit"):
            c.prepare(source, self.root / "object.7z", PASSWORD, limits=c.ContainerLimits(expanded_bytes=3))
        with patch.object(c.shutil, "disk_usage", return_value=type("Space", (), {"free": 0})()):
            with self.assertRaisesRegex(c.ContainerError, "insufficient_disk"):
                self.prepare(source)
        archive, descriptor = self.prepare(source)
        with self.assertRaisesRegex(c.ContainerError, "expanded_size_limit"):
            c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor, limits=c.ContainerLimits(expanded_bytes=3))
        self.assert_clean()
        with patch.object(c.shutil, "disk_usage", return_value=type("Space", (), {"free": 0})()):
            with self.assertRaisesRegex(c.ContainerError, "insufficient_disk"):
                c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)
        self.assert_clean()

    def test_manifest_and_entry_limits(self):
        source = self.root / "tree"; source.mkdir(); (source / "x").write_bytes(b"x")
        with self.assertRaisesRegex(c.ContainerError, "entry_limit"):
            c.prepare(source, self.root / "object.7z", PASSWORD, limits=c.ContainerLimits(entries=1))
        with self.assertRaisesRegex(c.ContainerError, "manifest_limit"):
            c.prepare(source, self.root / "object.7z", PASSWORD, limits=c.ContainerLimits(manifest_bytes=10))
        archive, descriptor = self.prepare(source)
        with self.assertRaisesRegex(c.ContainerError, "entry_limit"):
            c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor, limits=c.ContainerLimits(entries=1))
        self.assert_clean()

    def test_bounded_sink_checks_actual_writes(self):
        factory = c._factory(self.py7zr, self.root, {"data/x": {"type": "file", "size": 2}}, c.ContainerLimits())
        sink = factory.create("data/x")
        try:
            with self.assertRaisesRegex(c.ContainerError, "actual_write_limit"):
                sink.write(b"123")
            self.assertEqual(sink.size(), 0)
        finally:
            factory.close()

    def test_unsafe_paths_and_collisions(self):
        for name in ("../x", "/x", "C:/x", "//server/x", "a\\b", "a:b", "NUL.txt", "COM1", "a.", "a ", "a//b", "a/./b", "a\0b"):
            with self.subTest(name=name), self.assertRaises(c.ContainerError):
                c.valid_path(name)
        for names in (("A", "a"), ("é", "e\u0301"), ("A", "a/b")):
            with self.assertRaisesRegex(c.ContainerError, "name_collision"):
                c.validate_names({name: {"type": "directory"} for name in names})

    def test_hostile_headers_before_files_written(self):
        for name in ("../escape", "/absolute", "C:/escape", "data/CON", "data/x:stream"):
            with self.subTest(name=name):
                archive, descriptor = self.hostile([(name, b"x")])
                with self.assertRaises(c.ContainerError):
                    c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)
                self.assert_clean()
                self.assertFalse((self.root / "out").exists())

    def test_duplicate_case_unicode_and_symlink_archive(self):
        for entries in ([('data/a', b'x'), ('data/a', b'y')], [('data/A', b'x'), ('data/a', b'y')],
                        [('data/é', b'x'), ('data/e\u0301', b'y')]):
            archive, descriptor = self.hostile(entries)
            with self.assertRaises(c.ContainerError):
                c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)
            self.assert_clean()
        archive, descriptor = self.hostile([('data/link', b'../../outside')], attributes=((stat.S_IFLNK | 0o777) << 16) | 0x8000)
        with self.assertRaisesRegex(c.ContainerError, "links_or_special_member"):
            c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)

    def test_plaintext_header_or_payload_rejected(self):
        for header, encrypt, reason in ((False, True, "unencrypted_header"), (True, False, "unencrypted_content")):
            archive, descriptor = self.hostile([('data/a', b'x')], header=header, encrypt=encrypt)
            with self.assertRaisesRegex(c.ContainerError, reason):
                c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)

    def test_bad_manifest_and_digest_rejected(self):
        manifest = {"schema": 1, "root": "a", "entries": [{"path": "a", "type": "file", "size": 1, "sha256": "0" * 64}]}
        for value in ({"invalid": True}, manifest):
            archive, descriptor = self.hostile([('data/a', b'x')], manifest=value)
            with self.assertRaises(c.ContainerError):
                c.extract(archive, self.root / "out", PASSWORD, descriptor=descriptor)
            self.assertFalse((self.root / "out").exists())
            self.assert_clean()

    def test_encrypted_object_through_aqr2_and_cli_retry(self):
        source = self.source(bytes(range(256)) * 20)
        bundle = self.root / "bundle"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(c, "password_input", return_value=PASSWORD), contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["pack", str(source), "--output", str(bundle)]), 0)
        stream = self.root / "encrypted.aqs"
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["prepare", str(bundle / "object.7z"), "--descriptor", str(bundle / "descriptor.json"),
                                   "--output", str(stream), "--chunk-size", "100"]), 0)
        receiver = Receiver(self.root / "received")
        with stream.open("rb") as reader:
            for raw in read_stream(reader):
                receiver.feed(raw)
        self.assertTrue(receiver.snapshot()["content_verified"])
        status = self.root / "status.json"
        status.write_text(json.dumps(receiver.snapshot()))
        with patch.object(c, "password_input", side_effect=["wrong", PASSWORD]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["unpack", str(receiver.directory / "object.bin"), "--descriptor", str(status), "--output", str(self.root / "out")]), 0)
        self.assertEqual((self.root / "out/source.bin").read_bytes(), source.read_bytes())
        self.assertNotIn(PASSWORD, stdout.getvalue() + stderr.getvalue() + status.read_text())
        self.assertNotIn("source.bin", (bundle / "descriptor.json").read_text())


if __name__ == "__main__":
    unittest.main()
