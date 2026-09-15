from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import struct
import tempfile
import unittest
import zlib
import contextlib
import io

from qr_transfer import legacy
from qr_transfer.interfaces import State
from qr_transfer.protocol import DATA, META, HEADER, MAX_PAYLOAD, Limits, Packet, ProtocolError, TransferDescriptor, decode, encode
from qr_transfer.simulator import replay, schedule
from qr_transfer.stream import Session, read_stream, write_stream
from qr_transfer.transfer import Receiver, packets, prepare_object

ROOT = Path(__file__).resolve().parents[1]
TID = bytes(range(16))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, data=b"\0\xff\x80hello world", chunk=4):
        path = self.root / "source.bin"
        path.write_bytes(data)
        return prepare_object(path, chunk_size=chunk)

    def receiver(self, name="state", **kwargs):
        return Receiver(self.root / name, **kwargs)

    def test_golden_vectors_and_independent_byte_reader(self):
        vectors = json.loads((ROOT / "tests/vectors/packets.json").read_text())
        for vector in vectors:
            raw = bytes.fromhex(vector["hex"])
            if vector["protocol"] == "AQR1":
                value = legacy.parse_packet(raw)
                self.assertEqual(value["payload"].hex(), vector["payload_hex"])
                self.assertEqual(legacy.make_packet(value["type"], value["file_id"], value["index"], value["total"], value["payload"]), raw)
                continue
            value = decode(raw)
            self.assertEqual(value.index, vector["index"])
            self.assertEqual(value.total, vector["total"])
            self.assertEqual(value.payload.hex(), vector["payload_hex"])
            self.assertEqual(encode(value), raw)
            # Independent offsets, no production Struct/encoder.
            self.assertEqual(int.from_bytes(raw[6:8], "big"), 44)
            self.assertEqual(int.from_bytes(raw[28:32], "big"), vector["index"])
            self.assertEqual(int.from_bytes(raw[36:40], "big"), len(value.payload))
            self.assertEqual(int.from_bytes(raw[40:44], "big"), zlib.crc32(raw[:40] + bytes(4) + raw[44:]) & 0xFFFFFFFF)

    def test_all_byte_corruptions_rejected(self):
        raw = encode(Packet(DATA, TID, 0, 1, b"\0\xff\x80binary"))
        for offset in range(len(raw)):
            corrupt = bytearray(raw)
            corrupt[offset] ^= 1
            with self.assertRaises(ProtocolError, msg=str(offset)):
                decode(bytes(corrupt))
        for length in range(len(raw)):
            with self.assertRaises(ProtocolError):
                decode(raw[:length])
        with self.assertRaises(ProtocolError):
            decode(raw + b"\0")

    def test_bounds_without_large_allocations(self):
        for index in (65535, 65536, 0xFFFFFFFE):
            value = Packet(DATA, TID, index, 0xFFFFFFFF, b"x")
            self.assertEqual(decode(encode(value)), value)
        for packet in (Packet(DATA, TID, 1, 1, b"x"), Packet(DATA, TID, 0, 0, b"x"),
                       Packet(DATA, TID, 0, 1, b""), Packet(DATA, TID, 0, 1, b"x" * (MAX_PAYLOAD + 1))):
            with self.assertRaises(ProtocolError):
                encode(packet)
        self.assertEqual(len(encode(Packet(DATA, TID, 0, 1, b"x" * MAX_PAYLOAD))), 2953)

    def test_unknown_extensions_rejected(self):
        raw = encode(Packet(DATA, TID, 0, 1, b"x"))
        for offset in (4, 5, 9, 11):
            value = bytearray(raw)
            value[offset] = 99
            value[40:44] = bytes(4)
            value[40:44] = struct.pack(">I", zlib.crc32(value) & 0xFFFFFFFF)
            with self.assertRaisesRegex(ProtocolError, "unsupported"):
                decode(bytes(value))

    def test_descriptor_strict_types_fields_and_limits(self):
        descriptor = self.fixture().descriptor
        obj = descriptor.__dict__
        for key, value in (("object_size", True), ("chunk_size", 0), ("total", 999),
                           ("container", "zip"), ("sha256", "bad"), ("descriptor_version", 2)):
            with self.assertRaises(ProtocolError):
                TransferDescriptor.from_bytes(json.dumps({**obj, key: value}).encode())
        for raw in (b"{}", b"[]", b"\xff", b'{"total":1,"total":2}',
                    json.dumps({**obj, "filename": "secret"}).encode(), b" " * 1025):
            with self.assertRaises(ProtocolError):
                TransferDescriptor.from_bytes(raw)
        with self.assertRaises(ProtocolError):
            descriptor.validate(Limits(max_object=1))

    def test_roundtrip_boundaries_and_shuffle(self):
        for size in (1, 3, 4, 5, 8, 9, 255):
            transfer = self.fixture(bytes(range(size)), 4)
            sequence = list(packets(transfer))
            sequence += sequence[:]
            random.Random(size).shuffle(sequence)
            receiver = self.receiver(str(size))
            for raw in sequence:
                receiver.feed(raw)
            self.assertEqual(receiver.state, State.VERIFIED)
            self.assertEqual((receiver.directory / "object.bin").read_bytes(), bytes(range(size)))

    def test_empty_object_rejected_and_random_ids(self):
        with self.assertRaises(ProtocolError):
            self.fixture(b"")
        first = self.fixture()
        second = prepare_object(first.object_path)
        self.assertNotEqual(first.transfer_id, second.transfer_id)

    def test_incomplete_conflicts_foreign_and_cancel(self):
        sequence = list(packets(self.fixture()))
        receiver = self.receiver()
        receiver.feed(sequence[0])
        receiver.feed(sequence[1])
        packet = decode(sequence[1])
        self.assertEqual(receiver.feed(sequence[1]).reason, "duplicate")
        self.assertEqual(receiver.feed(encode(replace(packet, payload=b"z" * len(packet.payload)))).reason, "conflict")
        self.assertEqual(receiver.feed(encode(replace(packet, transfer_id=TID))).reason, "foreign_session")
        descriptor = replace(receiver.descriptor, sha256="0" * 64)
        self.assertEqual(receiver.feed(encode(Packet(META, packet.transfer_id, 0, packet.total, descriptor.to_bytes()))).reason, "conflict")
        self.assertEqual(receiver.state, State.RECEIVING)
        self.assertFalse((receiver.directory / "object.bin").exists())
        self.assertEqual(len(receiver.received), 1)
        receiver.cancel()
        self.assertEqual(receiver.feed(sequence[2]).reason, "inactive")
        self.assertFalse(receiver.snapshot()["content_verified"])

    def test_premetadata_quotas_expiry_and_selection(self):
        now = [0.0]
        receiver = self.receiver(limits=Limits(pending_packets=2, pending_sessions=1, pending_seconds=1), clock=lambda: now[0])
        sequence = list(packets(self.fixture()))
        receiver.feed(sequence[1])
        self.assertEqual(receiver.feed(sequence[1]).reason, "duplicate")
        foreign = replace(decode(sequence[1]), transfer_id=TID)
        self.assertEqual(receiver.feed(encode(foreign)).reason, "limit")
        receiver.feed(sequence[2])
        self.assertEqual(receiver.feed(sequence[3]).reason, "limit")
        now[0] = 2
        receiver.feed(sequence[0])
        self.assertEqual(len(receiver.received), 0)
        self.assertEqual(receiver.counters["expired"], 2)
        for raw in sequence[1:]:
            receiver.feed(raw)
        self.assertEqual(receiver.state, State.VERIFIED)

    def test_hash_failure_is_not_success(self):
        sequence = list(packets(self.fixture()))
        sequence[-1] = encode(replace(decode(sequence[-1]), payload=b"x" * len(decode(sequence[-1]).payload)))
        receiver = self.receiver()
        for raw in sequence:
            receiver.feed(raw)
        self.assertEqual(receiver.state, State.ERROR)
        self.assertFalse((receiver.directory / "object.bin").exists())

    def test_resume_and_exclusive_state(self):
        sequence = list(packets(self.fixture()))
        directory = self.root / "session"
        with Session(directory) as session:
            session.receiver.feed(sequence[0])
            session.receiver.feed(sequence[1])
            with self.assertRaises(FileExistsError):
                Session(directory, resume=True)
        with Session(directory, resume=True) as session:
            self.assertEqual(len(session.receiver.received), 1)
            for raw in sequence[1:]:
                session.receiver.feed(raw)
            self.assertEqual(session.receiver.state, State.VERIFIED)
        self.assertEqual(json.loads((directory / "status.json").read_text())["state"], "object_verified")
        with self.assertRaises(FileExistsError):
            Session(directory)

    def test_truncated_journal_and_stream_fail(self):
        directory = self.root / "session"
        with Session(directory):
            pass
        with (directory / "accepted.aqs").open("ab") as target:
            target.write(b"\0\0")
        with self.assertRaises(ProtocolError):
            Session(directory, resume=True)
        self.assertFalse((directory / ".lock").exists())

    def test_cli_incomplete_resume_and_corrupt_stream_exit_codes(self):
        from qr_transfer.cli import main
        sequence = list(packets(self.fixture()))
        first, full, bad = [self.root / name for name in ("first.aqs", "full.aqs", "bad.aqs")]
        write_stream(first, sequence[:2])
        write_stream(full, sequence)
        bad.write_bytes(b"AQS1\0\0")
        state = self.root / "cli-state"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["receive", str(first), "--state", str(state)]), 2)
            self.assertEqual(main(["resume", str(full), "--state", str(state)]), 0)
            self.assertEqual(main(["receive", str(bad), "--state", str(self.root / "bad-state")]), 1)
        self.assertEqual(json.loads((self.root / "bad-state/status.json").read_text())["state"], "error")

    def test_legacy_assembly_without_optional_dependencies(self):
        payload = bytes(range(256))
        meta = {"filename": "../ignored.bin", "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(), "protocol": "AQR1"}
        first = legacy.make_packet(0, b"12345678", 0, 1, json.dumps(meta).encode())
        data = legacy.make_packet(1, b"12345678", 0, 1, payload)
        self.assertEqual(legacy.assemble([first, data, data]), payload)
        with self.assertRaisesRegex(ProtocolError, "incomplete"):
            legacy.assemble([first])
        with self.assertRaises(ProtocolError):
            legacy.assemble([first, data], max_bytes=1)

    def test_simulation_repeat_replay_and_permanent_loss(self):
        sequence = list(packets(self.fixture(bytes(range(128)), 16), cycles=5))
        plan = schedule(len(sequence), seed=42, loss=.15, corrupt=.1, duplicate=.2, late=3, burst_start=10, burst_length=3)
        self.assertEqual(plan, schedule(len(sequence), seed=42, loss=.15, corrupt=.1, duplicate=.2, late=3, burst_start=10, burst_length=3))
        delivery = list(replay(sequence, plan))
        self.assertEqual(delivery, list(replay(sequence, json.loads(json.dumps(plan)))))
        receiver = self.receiver()
        for raw in delivery:
            receiver.feed(raw)
        self.assertEqual(receiver.state, State.VERIFIED)
        missing = self.receiver("missing")
        for raw in sequence:
            packet = decode(raw)
            if packet.kind == META or packet.index != 1:
                missing.feed(raw)
        self.assertNotEqual(missing.state, State.VERIFIED)

    def test_legacy_matches_original_and_qr_binary(self):
        if importlib.util.find_spec("qrcode") is None or importlib.util.find_spec("zxingcpp") is None:
            self.skipTest("Optional QR dependencies unavailable")
        from qr_transfer.visual import encode_qr, decode_qr
        spec = importlib.util.spec_from_file_location("legacy_sender", ROOT / "code.py")
        sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sender)
        payload = bytes(range(256))
        raw = legacy.make_packet(1, b"12345678", 0, 1, payload)
        self.assertEqual(raw, sender.make_packet(1, b"12345678", 0, 1, payload))
        self.assertEqual(legacy.parse_packet(raw)["payload"], payload)
        metadata = json.dumps({"filename": "../ignored.bin", "size": len(payload),
                               "sha256": hashlib.sha256(payload).hexdigest(), "protocol": "AQR1"}).encode()
        meta_packet = legacy.make_packet(0, b"12345678", 0, 1, metadata)
        self.assertEqual(legacy.assemble([meta_packet, raw, raw]), payload)
        for value in (raw, encode(Packet(DATA, TID, 0, 1, payload)), encode(Packet(DATA, TID, 0, 1, b"\xff" * MAX_PAYLOAD))):
            self.assertEqual(decode_qr(encode_qr(value, box_size=4)), [value])


if __name__ == "__main__":
    unittest.main()
