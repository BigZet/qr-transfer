from __future__ import annotations
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest

from qr_transfer.fec import (
    Bootstrap,
    FecSession,
    bootstrap,
    pool,
    decode,
    encode,
    SYMBOL,
)
from qr_transfer.protocol import ProtocolError, decode as decode_repeat
from qr_transfer.transfer import prepare_object
from qr_transfer.capture import receive_frames


class FecTests(unittest.TestCase):
    def test_published_vector(self):
        vector = json.loads((Path(__file__).parent / "vectors/aqr2-lt-v1.json").read_text())
        archive = self.root / "vector.7z"
        archive.write_bytes(bytes.fromhex(vector["archive_hex"]))
        meta = decode(bytes.fromhex(vector["bootstrap_hex"]))
        boot = Bootstrap.from_bytes(meta.payload)
        sequence = list(pool(archive, boot, meta.transfer_id))
        self.assertEqual(sequence[0].hex(), vector["bootstrap_hex"])
        self.assertEqual(sequence[1].hex(), vector["source_hex"])
        self.assertEqual(next(p for p in sequence if decode(p).kind == 2).hex(), vector["first_repair_hex"])

    def test_insufficient_finite_pool_does_not_become_success(self):
        boot = bootstrap(self.transfer.descriptor, repair_factor=1, mode="repair-only")
        sequence = list(pool(self.source, boot, self.transfer.transfer_id))
        with FecSession(self.root / "insufficient") as state:
            for _ in range(3):
                for raw in sequence:
                    state.feed(raw)
            self.assertEqual(state.committed, boot.symbols)
            self.assertFalse(state.snapshot()["content_verified"])
            self.assertFalse((state.directory / "object.bin").exists())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.write_bytes(hashlib.shake_256(b"i05-test").digest(200000))
        self.transfer = prepare_object(self.source, container="7z-aes256")

    def sequence(self, mode="systematic"):
        boot = bootstrap(self.transfer.descriptor, mode=mode)
        return boot, list(pool(self.source, boot, self.transfer.transfer_id))

    def test_profiles_do_not_mix_and_crc(self):
        b, seq = self.sequence()
        for raw in seq[:4]:
            self.assertEqual(encode(decode(raw)), raw)
            with self.assertRaises(ProtocolError):
                decode_repeat(raw)
        raw = bytearray(seq[2])
        raw[-1] ^= 1
        with self.assertRaisesRegex(ProtocolError, "crc"):
            decode(bytes(raw))

    def test_late_metadata_losses_duplicates_and_resume(self):
        b, seq = self.sequence()
        rng = random.Random(42)
        delivery = [p for p in seq[1:] if rng.random() > 0.3]
        rng.shuffle(delivery)
        path = self.root / "state"
        with FecSession(path) as state:
            for raw in delivery[:20]:
                state.feed(raw)
            state.feed(seq[0])
            state.feed(seq[0])
            for raw in delivery[20:60]:
                state.feed(raw)
        with FecSession(path, resume=True) as state:
            for raw in delivery[60:]:
                state.feed(raw)
                state.feed(raw)
            self.assertTrue(state.snapshot()["content_verified"])
            self.assertLessEqual(len(state.cache), 8)
        self.assertEqual((path / "object.bin").read_bytes(), self.source.read_bytes())

    def test_repair_only_and_finite_pool(self):
        b, seq = self.sequence("repair-only")
        self.assertTrue(all(decode(p).kind == 2 for p in seq[1:]))
        with FecSession(self.root / "repair") as state:
            for raw in reversed(seq):
                state.feed(raw)
            # Pre-metadata buffer is deliberately bounded; replay finite pool.
            for raw in seq:
                state.feed(raw)
            self.assertTrue(state.snapshot()["content_verified"])
            before = state.snapshot()["state_bytes"]
            for raw in seq:
                state.feed(raw)
            self.assertEqual(state.snapshot()["state_bytes"], before)

    def test_parameter_limits_and_conflict(self):
        b, seq = self.sequence()
        for bad in (
            replace(b, k=0),
            replace(b, repair_factor=9),
            replace(b, symbol_size=1024),
            replace(b, scheme="unknown"),
        ):
            with self.assertRaises(ProtocolError):
                bad.to_bytes()
        with FecSession(self.root / "conflict") as state:
            state.feed(seq[0])
            state.feed(seq[1])
            packet = decode(seq[1])
            changed = encode(replace(packet, payload=b"z" * len(packet.payload)))
            self.assertEqual(state.feed(changed).reason, "conflict")

    def test_false_data_cannot_complete(self):
        b, seq = self.sequence()
        with FecSession(self.root / "bad") as state:
            with self.assertRaises(ProtocolError):
                for raw in seq:
                    packet = decode(raw)
                    if packet.kind == 1:
                        raw = encode(replace(packet, payload=b"x" * SYMBOL))
                    state.feed(raw)
            self.assertFalse(state.snapshot()["content_verified"])

    def test_fec_disk_limit_corrupt_state_and_writer(self):
        import sqlite3
        from unittest.mock import patch

        b, seq = self.sequence()
        path = self.root / "disk"
        with FecSession(path) as state:
            state.feed(seq[0])
            with self.assertRaises(FileExistsError):
                FecSession(path, resume=True)
            with patch("qr_transfer.fec.shutil.disk_usage") as space:
                space.return_value.free = 0
                with self.assertRaisesRegex(ProtocolError, "insufficient_disk"):
                    state.feed(seq[1])
            self.assertEqual(state.committed, 0)
        with FecSession(path, resume=True) as state:
            self.assertEqual(state.committed, 0)
        db = sqlite3.connect(path / "fec.sqlite3")
        try:
            db.execute("UPDATE packets SET raw=?", (b"bad",))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ProtocolError):
            FecSession(path, resume=True)

    def test_capture_adapter_and_completed_resume(self):
        b, seq = self.sequence()
        it = iter([[p] for p in seq])
        result = receive_frames(
            self.root / "capture", lambda: next(it), lambda x: x, transport="lt", fps=60
        )
        self.assertEqual(result["exit_code"], 0)
        result = receive_frames(
            self.root / "capture",
            lambda: self.fail("grab"),
            lambda x: x,
            transport="lt",
            resume=True,
        )
        self.assertEqual(result["exit_code"], 0)

    def test_cache_eviction_and_backend_replay(self):
        self.source.write_bytes(hashlib.shake_256(b"large").digest(850000))
        self.transfer = prepare_object(self.source, container="7z-aes256")
        b, seq = self.sequence()
        # Spread across >8 blocks to force reload from SQLite.
        data = sorted(
            seq[1:],
            key=lambda p: (
                decode(p).index & 0xFFFFFF,
                decode(p).index >> 24,
                decode(p).kind,
            ),
        )
        with FecSession(self.root / "eviction") as state:
            state.feed(seq[0])
            for raw in data:
                state.feed(raw)
                self.assertLessEqual(len(state.cache), 8)
                if state.snapshot()["content_verified"]:
                    break
            self.assertTrue(state.snapshot()["content_verified"])


if __name__ == "__main__":
    unittest.main()
