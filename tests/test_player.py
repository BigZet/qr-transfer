from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

from qr_transfer.capture import receive_frames, validate_options
from qr_transfer.player import HEADER, FRAME_BYTES, estimate, render
from qr_transfer.protocol import TransferDescriptor
from qr_transfer.transfer import prepare_object, packets


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, data=b"\0\xff\x80test", chunk=4):
        path = self.root / "synthetic-object.bin"
        path.write_bytes(data)
        # Synthetic transport-only fixture, not an encryption assertion.
        return prepare_object(path, chunk_size=chunk, container="7z-aes256")

    def run_capture(self, sequence, *, increment=.1, **options):
        now = [0.0]
        iterator = iter(sequence)
        def grab():
            now[0] += increment
            return next(iterator, None)
        def sleep(seconds):
            now[0] += seconds
        return receive_frames(self.root / "state", grab, lambda value: value,
                              clock=lambda: now[0], sleep=sleep, **options)

    def test_budget_metadata_overhead_and_size_limits(self):
        descriptor = self.fixture(b"x" * 64, 8).descriptor
        budget = estimate(descriptor)
        self.assertEqual(budget["unique_frames"], 9)
        self.assertEqual(budget["cycle_frames"], 10)
        self.assertEqual(budget["matrix_bytes"], HEADER.size + 9 * FRAME_BYTES)
        self.assertEqual(budget["cycle_seconds"], 6)
        large = TransferDescriptor(64 << 20, "0" * 64, 2800, math.ceil((64 << 20) / 2800), "7z-aes256")
        self.assertTrue(estimate(large)["external_supported"])
        for interval in (0, 49, 10001):
            with self.assertRaises(ValueError):
                estimate(descriptor, interval_ms=interval)

    @unittest.skipUnless(importlib.util.find_spec("segno"), "Install .[sender]")
    def test_standalone_and_separate_assets(self):
        transfer = self.fixture()
        out = self.root / "player"
        result = render(transfer.object_path, transfer.descriptor, out)
        raw = (out / "frames.bin").read_bytes()
        self.assertEqual(HEADER.unpack_from(raw), (b"QRM1", 177, 4, transfer.descriptor.total + 1))
        self.assertEqual(result["matrix_bytes"], len(raw))
        html = (out / "standalone.html").read_text(encoding="utf-8")
        self.assertNotIn('<script src=', html)
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("synthetic-object.bin", html)
        self.assertNotIn("https://", html)
        self.assertNotIn('<script src="player.js">', (out / "index.html").read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            render(transfer.object_path, transfer.descriptor, out)

    def test_capture_late_join_duplicates_and_completion(self):
        transfer = self.fixture()
        sequence = list(packets(transfer))
        result = self.run_capture([None, sequence[2], sequence[2], b"bad"] + sequence)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["transfer"]["received_bytes"], transfer.descriptor.object_size)
        self.assertTrue(result["transfer"]["counters"]["duplicate"])
        self.assertEqual((self.root / "state/object.bin").read_bytes(), transfer.object_path.read_bytes())

    @unittest.skipUnless(importlib.util.find_spec("segno"), "Install .[sender]")
    def test_refresh_index_preserves_transfer_and_rejects_bad_frames(self):
        from tools.refresh_player import refresh, ConfigParser
        transfer = self.fixture()
        out = self.root / "refresh-player"
        generated = render(transfer.object_path, transfer.descriptor, out, standalone=False)
        frames = (out / "frames.bin").read_bytes()
        old = (out / "index.html").read_bytes()
        result = refresh(out, paged=True, part_frames=8)
        self.assertEqual(Path(result["backup"]).read_bytes(), old)
        self.assertEqual((out / "frames.bin").read_bytes(), frames)
        self.assertEqual(result["transfer_id"], generated["transfer_id"])
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('<script src=', html)
        parser = ConfigParser()
        parser.feed(html)
        self.assertEqual(json.loads("".join(parser.parts))["transfer_id"], result["transfer_id"])
        self.assertEqual(json.loads("".join(parser.parts))["parts"]["frames_per_part"], 8)
        (out / "frames.bin").write_bytes(frames[:-1] + bytes([frames[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "checksum"):
            refresh(out)
        self.assertEqual((out / "index.html").read_text(encoding="utf-8"), html)

    def test_matrix_pages_reassemble_exactly_and_detect_corruption(self):
        import zlib
        from qr_transfer.player import partition_matrices
        # Frame contents need not be valid QR to check paging integrity/layout.
        raw = HEADER.pack(b"QRM1",177,4,19) + bytes(range(256)) * (19*FRAME_BYTES//256) + bytes(range(19*FRAME_BYTES%256))
        path = self.root / "frames.bin"
        path.write_bytes(raw)
        manifest = partition_matrices(path, frames_per_part=8, expected_crc=zlib.crc32(raw)&0xffffffff)
        parts = [(self.root/manifest["directory"]/f"{i:06d}.bin").read_bytes() for i in range(3)]
        self.assertEqual(b"".join(parts), raw[12:])
        self.assertEqual([len(p)//FRAME_BYTES for p in parts], [8,8,3])
        for blob,item in zip(parts,manifest["items"]):
            self.assertEqual(zlib.crc32(blob)&0xffffffff, item["crc32"])
        with self.assertRaisesRegex(ValueError,"checksum"):
            partition_matrices(path, expected_crc=0)
        for size in (0,7,1025):
            with self.assertRaises(ValueError):
                partition_matrices(path, frames_per_part=size)

    def test_first_timeout_and_invalid_packets(self):
        result = self.run_capture([b"noise"] * 30, first_timeout=1)
        self.assertEqual(result["termination"], "first_timeout")
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["transfer"]["content_verified"])

    def test_duplicates_do_not_prevent_idle_timeout(self):
        first = next(packets(self.fixture()))
        result = self.run_capture([first] * 50, idle_timeout=1)
        self.assertEqual(result["termination"], "idle_timeout")
        self.assertFalse(result["transfer"]["content_verified"])

    def test_total_timeout_is_separate(self):
        result = self.run_capture([None] * 100, total_timeout=.5, first_timeout=10)
        self.assertEqual(result["termination"], "total_timeout")

    def test_default_allows_more_than_300_seconds(self):
        result = self.run_capture(list(packets(self.fixture(b"x" * 350, 1))), increment=1)
        self.assertEqual(result["exit_code"], 0)
        self.assertGreater(result["elapsed_seconds"], 300)

    def test_capture_cancel_and_error_preserve_reports(self):
        def cancelled():
            raise KeyboardInterrupt
        result = receive_frames(self.root / "cancel", cancelled, lambda x: x)
        self.assertEqual(result["exit_code"], 130)
        self.assertEqual(result["transfer"]["state"], "cancelled")
        def failed():
            raise RuntimeError("capture backend failed")
        result = receive_frames(self.root / "error", failed, lambda x: x)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(json.loads((self.root / "error/status.json").read_text())["state"], "error")

    def test_invalid_capture_options(self):
        for values in ((0,12,120,180,0), (1,0,120,180,0), (1,-1,120,180,0),
                       (1,float('nan'),120,180,0), (1,12,0,180,0), (1,12,120,-1,0), (1,12,120,180,-1)):
            with self.assertRaises(ValueError):
                validate_options(*values)


if __name__ == "__main__":
    unittest.main()
