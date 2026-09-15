"""Local test/journal framing, not an on-screen wire protocol."""
from __future__ import annotations

from pathlib import Path
import json
import struct

from .protocol import Limits, MAX_PACKET, ProtocolError
from .transfer import Receiver

STREAM_MAGIC = b"AQS1"


def write_record(target, raw: bytes):
    if not 1 <= len(raw) <= MAX_PACKET:
        raise ProtocolError("length")
    target.write(struct.pack(">I", len(raw)) + raw)


def read_stream(source, *, max_records: int = 1_000_000):
    if source.read(4) != STREAM_MAGIC:
        raise ProtocolError("stream_magic")
    for _ in range(max_records):
        size = source.read(4)
        if not size:
            return
        if len(size) != 4:
            raise ProtocolError("stream_truncated")
        length = struct.unpack(">I", size)[0]
        if not 1 <= length <= MAX_PACKET:
            raise ProtocolError("limit")
        raw = source.read(length)
        if len(raw) != length:
            raise ProtocolError("stream_truncated")
        yield raw
    if source.read(1):
        raise ProtocolError("limit")


def write_stream(path: Path, sequence):
    with path.open("xb") as target:
        target.write(STREAM_MAGIC)
        for raw in sequence:
            write_record(target, raw)


class Session:
    """Resume a closed test session by replaying accepted packets.

    Pending pre-metadata frames are ephemeral. Torn journals fail explicitly;
    transactional crash recovery and faster indexed state belong to iteration 04.
    Only one process may own a session at a time.
    """

    def __init__(self, directory: Path, *, resume: bool = False, limits: Limits = Limits()):
        self.directory = directory
        if not resume:
            directory.mkdir(parents=True, exist_ok=False)
        self.lock = directory / ".lock"
        self.lock_handle = self.lock.open("x")
        self.journal = None
        try:
            self.receiver = Receiver(directory, limits=limits)
            journal = directory / "accepted.aqs"
            if resume:
                with journal.open("rb") as source:
                    for raw in read_stream(source, max_records=limits.max_chunks + 1):
                        event = self.receiver.feed(raw)
                        if event.reason not in ("metadata", "accepted") or self.receiver.state.value == "error":
                            raise ProtocolError("journal_invalid")
                self.journal = journal.open("ab")
            else:
                self.journal = journal.open("xb")
                self.journal.write(STREAM_MAGIC)
                self.journal.flush()
            self.receiver.record = self.record
        except BaseException:
            if self.journal:
                self.journal.close()
            self.lock_handle.close()
            self.lock.unlink()
            raise

    def record(self, raw):
        write_record(self.journal, raw)
        self.journal.flush()

    def close(self):
        try:
            self.journal.close()
            snapshot = self.directory / "status.tmp"
            snapshot.write_text(json.dumps(self.receiver.snapshot(), indent=2) + "\n", encoding="utf-8")
            snapshot.replace(self.directory / "status.json")
        finally:
            self.lock_handle.close()
            self.lock.unlink()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
