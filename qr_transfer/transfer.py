"""Streaming packet preparation and a bounded, disk-backed repeat receiver."""
from __future__ import annotations

from collections import Counter, OrderedDict
from pathlib import Path
import os
import time

from .interfaces import DecodeEvent, PreparedTransfer, State
from .protocol import DATA, META, Limits, Packet, ProtocolError, TransferDescriptor, decode, encode, sha256_file


def prepare_object(path: Path, *, chunk_size: int = 2800, container: str = "opaque-test",
                   limits: Limits = Limits()) -> PreparedTransfer:
    size = path.stat().st_size
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ProtocolError("limit")
    descriptor = TransferDescriptor(size, "0" * 64, chunk_size, (size + chunk_size - 1) // chunk_size, container)
    descriptor.validate(limits)  # Check quotas before hashing.
    descriptor = TransferDescriptor(size, sha256_file(path), chunk_size, descriptor.total, container)
    return PreparedTransfer(path, os.urandom(16), descriptor)


def packets(transfer: PreparedTransfer, *, cycles: int = 1, metadata_every: int = 8):
    if cycles < 1 or metadata_every < 1:
        raise ValueError("Positive cycles and metadata interval required")
    descriptor = transfer.descriptor
    metadata = encode(Packet(META, transfer.transfer_id, 0, descriptor.total, descriptor.to_bytes()))
    for _ in range(cycles):
        yield metadata
        with transfer.object_path.open("rb") as source:
            for index in range(descriptor.total):
                payload = source.read(descriptor.chunk_size)
                expected = min(descriptor.chunk_size, descriptor.object_size - index * descriptor.chunk_size)
                if len(payload) != expected:
                    raise ValueError("Prepared object changed")
                yield encode(Packet(DATA, transfer.transfer_id, index, descriptor.total, payload))
                if (index + 1) % metadata_every == 0:
                    yield metadata
            if source.read(1):
                raise ValueError("Prepared object changed")
        if sha256_file(transfer.object_path) != descriptor.sha256:
            raise ValueError("Prepared object changed")


class Receiver:
    """One active session; validated pre-metadata candidates have small TTL quotas.

    Caller owns a private working directory. Journal/resume is provided by Session.
    No names from metadata are used as paths. This does not decrypt or extract.
    """

    def __init__(self, directory: Path, *, limits: Limits = Limits(), clock=time.monotonic, record=None):
        self.directory = directory
        (directory / "chunks").mkdir(parents=True, exist_ok=True)
        self.limits, self.clock, self.record = limits, clock, record
        self.state = State.WAITING
        self.descriptor = None
        self.transfer_id = None
        self.received = set()
        self.counters = Counter()
        self.pending = OrderedDict()

    def _event(self, reason):
        self.counters[reason] += 1
        return DecodeEvent(reason, self.state.value, len(self.received),
                           self.descriptor.total if self.descriptor else None)

    def snapshot(self):
        return {"schema": 1, "state": self.state.value,
                "transfer_id": self.transfer_id.hex() if self.transfer_id else None,
                "descriptor": dict(self.descriptor.__dict__) if self.descriptor else None,
                "received_chunks": len(self.received), "pending_packets": len(self.pending),
                "counters": dict(self.counters), "content_verified": self.state == State.VERIFIED,
                "encryption_verified": False}

    def cancel(self):
        self.state = State.CANCELLED
        self.pending.clear()

    def feed(self, raw: bytes) -> DecodeEvent:
        if self.state in (State.CANCELLED, State.ERROR):
            return self._event("inactive")
        try:
            packet = decode(raw)
            if packet.total > self.limits.max_chunks:
                raise ProtocolError("limit")
            descriptor = TransferDescriptor.from_bytes(packet.payload, self.limits) if packet.kind == META else None
            if descriptor and descriptor.total != packet.total:
                raise ProtocolError("format")
        except ProtocolError as error:
            return self._event(error.reason)
        if self.transfer_id is not None and packet.transfer_id != self.transfer_id:
            return self._event("foreign_session")
        if descriptor:
            if self.descriptor:
                return self._event("duplicate" if descriptor == self.descriptor else "conflict")
            self.transfer_id, self.descriptor = packet.transfer_id, descriptor
            self.state = State.RECEIVING
            if self.record:
                self.record(raw)
            queued = list(self.pending.values())
            self.pending.clear()
            for when, buffered in queued:
                if self.clock() - when <= self.limits.pending_seconds:
                    self.feed(buffered)
                else:
                    self.counters["expired"] += 1
            return self._event("metadata")
        if self.descriptor is None:
            now = self.clock()
            for key, (when, _) in list(self.pending.items()):
                if now - when > self.limits.pending_seconds:
                    del self.pending[key]
                    self.counters["expired"] += 1
            key = (packet.transfer_id, packet.index)
            if key in self.pending:
                return self._event("duplicate" if self.pending[key][1] == raw else "conflict")
            sessions = {key[0] for key in self.pending}
            if len(self.pending) >= self.limits.pending_packets or (packet.transfer_id not in sessions and len(sessions) >= self.limits.pending_sessions):
                return self._event("limit")
            self.pending[key] = (now, raw)
            self.state = State.BUFFERING
            return self._event("buffered")
        descriptor = self.descriptor
        if packet.total != descriptor.total:
            return self._event("conflict")
        expected = min(descriptor.chunk_size, descriptor.object_size - packet.index * descriptor.chunk_size)
        if len(packet.payload) != expected:
            return self._event("length")
        path = self.directory / "chunks" / f"{packet.index:08x}.bin"
        if packet.index in self.received:
            return self._event("duplicate" if path.read_bytes() == packet.payload else "conflict")
        with path.open("wb") as target:
            target.write(packet.payload)
        if self.record:
            self.record(raw)
        self.received.add(packet.index)
        if len(self.received) == descriptor.total:
            self._assemble()
        return self._event("accepted")

    def _assemble(self):
        self.state = State.ASSEMBLING
        part = self.directory / "object.part"
        with part.open("wb") as target:
            for index in range(self.descriptor.total):
                with (self.directory / "chunks" / f"{index:08x}.bin").open("rb") as source:
                    target.write(source.read())
        self.state = State.VERIFYING
        if part.stat().st_size != self.descriptor.object_size or sha256_file(part) != self.descriptor.sha256:
            self.state = State.ERROR
            self.counters["hash"] += 1
            return
        part.replace(self.directory / "object.bin")
        self.state = State.VERIFIED
