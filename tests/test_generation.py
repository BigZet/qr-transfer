import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qr_transfer.generation import encoded_frames, worker_count
from qr_transfer.player import render, FRAME_BYTES
from qr_transfer.transfer import prepare_object


class GenerationTests(unittest.TestCase):
    def test_atomic_publication_retries_reader_lock(self):
        from qr_transfer.player import _atomic
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"state.json"
            path.write_bytes(b"old complete value")
            original = Path.replace
            calls = []
            def locked_once(source, target):
                calls.append(1)
                if len(calls) == 1:
                    self.assertEqual(path.read_bytes(), b"old complete value")
                    raise PermissionError("reader holds file")
                return original(source, target)
            with patch.object(Path, "replace", locked_once):
                _atomic(path, b"new complete value")
            self.assertEqual(path.read_bytes(), b"new complete value")
            self.assertEqual(len(calls), 2)

    def test_parallel_preserves_matrix_order(self):
        packets = [hashlib.shake_256(bytes([i])).digest(100+i) for i in range(5)]
        self.assertEqual(list(encoded_frames(packets, workers=1)), list(encoded_frames(packets, workers=2)))

    def test_worker_limits(self):
        self.assertTrue(1 <= worker_count(0) <= 4)
        self.assertEqual(worker_count(2), 2)
        for value in (-1, 17, True):
            with self.assertRaises(ValueError):
                worker_count(value)

    def test_live_publication_and_producer_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root/"object.bin"
            source.write_bytes(b"x"*64)
            transfer = prepare_object(source, chunk_size=1, container="7z-aes256")
            output = root/"player"
            def fail(*args, **kwargs):
                for _ in range(8):
                    yield b"\x00"*FRAME_BYTES
                self.assertTrue((output/"index.html").exists())
                raise RuntimeError("synthetic encoder failure")
            with patch("qr_transfer.generation.encoded_frames", fail):
                with self.assertRaisesRegex(RuntimeError,"synthetic"):
                    render(source, transfer.descriptor, output, standalone=False, live=True, part_frames=8)
            parts = next(output.glob("parts-*"))
            self.assertEqual(json.loads((parts/"generation.json").read_text())["state"], "failed")
            self.assertTrue((parts/"000000.bin").exists())
            self.assertTrue((parts/"000000.json").exists())
            self.assertFalse((parts/"000001.json").exists())
            self.assertFalse((output/"prepare.json").exists())

    def test_live_requires_external(self):
        with self.assertRaisesRegex(ValueError,"external"):
            render(Path("unused"), None, Path("unused"), live=True)


if __name__ == "__main__":
    unittest.main()
