"""AQR2 profile 2: bounded LT-code 0.3 source/repair pool, no custom fountain math."""

from __future__ import annotations
from collections import Counter, OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import time
import zlib

from .protocol import (
    HEADER,
    MAX_PACKET,
    MAX_METADATA,
    Packet,
    ProtocolError,
    TransferDescriptor,
    _unique_object,
    sha256_file,
)
from .interfaces import State, DecodeEvent
from .storage import WriterLock, RESERVE

PROFILE = 2
SYMBOL = 2800
PREFIX = 1024
MAX_OBJECT = 16 << 20
MAX_BLOCKS = 256
MAX_DB = 160 << 20


def decode(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise ProtocolError("format")
    if not HEADER.size <= len(raw) <= MAX_PACKET:
        raise ProtocolError("length")
    magic, kind, flags, hlen, profile, version, tid, index, total, length, crc = (
        HEADER.unpack_from(raw)
    )
    if zlib.crc32(raw[:40] + b"\0" * 4 + raw[44:]) & 0xFFFFFFFF != crc:
        raise ProtocolError("crc")
    if (magic, flags, hlen, profile, version) != (b"AQR2", 0, 44, PROFILE, 1):
        raise ProtocolError("unsupported")
    if length != len(raw) - 44 or kind not in (0, 1, 2) or not 1 <= total <= MAX_BLOCKS:
        raise ProtocolError("format")
    if kind == 0 and (index or length > MAX_METADATA):
        raise ProtocolError("format")
    if kind and (
        (index >> 24) >= total
        or (index & 0xFFFFFF) >= 128
        or length != SYMBOL + (4 if kind == 2 else 0)
    ):
        raise ProtocolError("format")
    if kind == 2 and not 1 <= int.from_bytes(raw[44:48], "big") < 2147483647:
        raise ProtocolError("format")
    return Packet(kind, tid, index, total, raw[44:])


def encode(packet):
    if len(packet.transfer_id) != 16:
        raise ProtocolError("format")
    raw = (
        HEADER.pack(
            b"AQR2",
            packet.kind,
            0,
            44,
            PROFILE,
            1,
            packet.transfer_id,
            packet.index,
            packet.total,
            len(packet.payload),
            0,
        )
        + packet.payload
    )
    raw = raw[:40] + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF) + raw[44:]
    decode(raw)
    return raw


@dataclass(frozen=True)
class Bootstrap:
    object: dict
    k: int
    repair_factor: int = 3
    mode: str = "systematic"
    scheme: str = "lt-code-0.3-v1"
    symbol_size: int = SYMBOL

    @property
    def descriptor(self):
        return TransferDescriptor.from_bytes(json.dumps(self.object).encode())

    @property
    def capacity(self):
        return self.k * SYMBOL - PREFIX

    @property
    def blocks(self):
        return math.ceil(self.descriptor.object_size / self.capacity)

    def data_size(self, block):
        return min(self.capacity, self.descriptor.object_size - block * self.capacity)

    def source_count(self, block):
        return math.ceil((self.data_size(block) + PREFIX) / SYMBOL)

    @property
    def symbols(self):
        return sum(
            self.source_count(b) * (self.repair_factor + (self.mode == "systematic"))
            for b in range(self.blocks)
        )

    def validate(self):
        descriptor = self.descriptor
        if descriptor.object_size > MAX_OBJECT or descriptor.container != "7z-aes256":
            raise ProtocolError("fec_object_limit")
        if (
            type(self.k) is not int
            or not 2 <= self.k <= 32
            or type(self.repair_factor) is not int
            or not 1 <= self.repair_factor <= 3
        ):
            raise ProtocolError("fec_parameters")
        if (
            self.mode not in ("systematic", "repair-only")
            or self.scheme != "lt-code-0.3-v1"
            or self.symbol_size != SYMBOL
        ):
            raise ProtocolError("unsupported")
        if self.blocks > MAX_BLOCKS:
            raise ProtocolError("fec_block_limit")

    def to_bytes(self):
        self.validate()
        raw = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > 900:
            raise ProtocolError("metadata_limit")
        return raw

    @classmethod
    def from_bytes(cls, raw):
        if len(raw) > 900:
            raise ProtocolError("metadata_limit")
        try:
            obj = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(obj, dict) or set(obj) != set(cls.__dataclass_fields__):
                raise ProtocolError("format")
            value = cls(**obj)
            value.validate()
            return value
        except (TypeError, UnicodeError, ValueError, RecursionError) as error:
            if isinstance(error, ProtocolError):
                raise
            raise ProtocolError("format") from error

    def prefix(self, block):
        raw = self.to_bytes()
        return (b"LTB1" + struct.pack(">II", block, len(raw)) + raw).ljust(
            PREFIX, b"\0"
        )


def bootstrap(descriptor, repair_factor=3, mode="systematic"):
    result = Bootstrap(
        descriptor.__dict__,
        min(32, max(2, math.ceil((descriptor.object_size + PREFIX) / SYMBOL))),
        repair_factor,
        mode,
    )
    result.validate()
    return result


def pool(archive, boot, tid):
    """One metadata + bounded windows of eight blocks, transposed by symbol rank."""
    from lt.encode import encoder

    yield encode(Packet(0, tid, 0, boot.blocks, boot.to_bytes()))
    with (
        archive.open("rb") as source,
        tempfile.TemporaryDirectory(prefix="qr-lt-") as work,
    ):
        for first in range(0, boot.blocks, 8):
            window = []
            for block in range(first, min(first + 8, boot.blocks)):
                data = source.read(boot.data_size(block))
                if len(data) != boot.data_size(block):
                    raise ProtocolError("object_changed")
                count = boot.source_count(block)
                envelope = (boot.prefix(block) + data).ljust(count * SYMBOL, b"\0")
                path = Path(work) / str(block)
                path.write_bytes(envelope)
                values = []
                if boot.mode == "systematic":
                    values = [
                        encode(
                            Packet(
                                1,
                                tid,
                                (block << 24) | i,
                                boot.blocks,
                                envelope[i * SYMBOL : (i + 1) * SYMBOL],
                            )
                        )
                        for i in range(count)
                    ]
                iterator = encoder(str(path), SYMBOL, seed=2067261, c=0.1, delta=0.5)
                for i in range(count * boot.repair_factor):
                    raw = next(iterator)
                    values.append(
                        encode(
                            Packet(
                                2,
                                tid,
                                (block << 24) | i,
                                boot.blocks,
                                raw[8:12] + raw[12:],
                            )
                        )
                    )
                iterator.close()
                path.unlink()
                split = count if boot.mode == "systematic" else 0
                window.append((values[:split], values[split:]))
            for phase in (0, 1):
                for rank in range(max(len(parts[phase]) for parts in window)):
                    for parts in window:
                        if rank < len(parts[phase]):
                            yield parts[phase][rank]
        if source.read(1) or sha256_file(archive) != boot.descriptor.sha256:
            raise ProtocolError("object_changed")


class FecSession:
    """SQLite stores finite symbols; decoder cache is bounded to eight source blocks."""

    def __init__(self, directory, *, resume=False):
        self.directory = Path(directory)
        if not resume:
            self.directory.mkdir(parents=True, exist_ok=False)
        elif not self.directory.is_dir():
            raise ProtocolError("session_missing")
        self.lock = WriterLock(self.directory / ".writer.lock")
        self.db = None
        self.receiver = self
        self.state = State.WAITING
        self.descriptor = None
        self.boot = None
        self.transfer_id = None
        self.received_bytes = 0
        self.counters = Counter()
        self.pending = OrderedDict()
        self.cache = OrderedDict()
        self.completed = set()
        self.committed = 0
        start = time.perf_counter()
        try:
            path = self.directory / "fec.sqlite3"
            if resume and not path.exists():
                raise ProtocolError("fec_session_required")
            if path.exists() and path.stat().st_size > MAX_DB:
                raise ProtocolError("state_limit")
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA cache_size=-2048")
            if not resume:
                self.db.execute(
                    "CREATE TABLE packets(kind INTEGER, idx INTEGER, raw BLOB, PRIMARY KEY(kind,idx))"
                )
                self.db.execute("PRAGMA user_version=1")
                self.db.commit()
            elif self.db.execute("PRAGMA user_version").fetchone()[
                0
            ] != 1 or self.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ProtocolError("state_corrupt")
            (self.directory / "blocks").mkdir(exist_ok=True)
            if resume:
                count, largest = self.db.execute(
                    "SELECT count(*),coalesce(max(length(raw)),0) FROM packets"
                ).fetchone()
                if count > 65536 or largest > MAX_PACKET:
                    raise ProtocolError("state_limit")
                row = self.db.execute(
                    "SELECT raw FROM packets WHERE kind=0 AND idx=0"
                ).fetchone()
                if row:
                    meta = decode(row[0])
                    self._metadata(meta)
                    sizes = self.db.execute(
                        "SELECT count(*),coalesce(max(length(raw)),0) FROM packets"
                    ).fetchone()
                    if sizes[0] > self.boot.symbols + 1 or sizes[1] > MAX_PACKET:
                        raise ProtocolError("state_limit")
                    # Replay one block at a time. Bounded memory; don't trust cached files.
                    for block in range(self.boot.blocks):
                        for kind, idx, raw in self.db.execute(
                            "SELECT kind,idx,raw FROM packets WHERE kind>0 AND idx>=? AND idx<? ORDER BY kind,idx",
                            (block << 24, (block + 1) << 24),
                        ):
                            packet = decode(raw)
                            self._validate(packet)
                            if (kind, idx) != (packet.kind, packet.index):
                                raise ProtocolError("state_corrupt")
                            self.committed += 1
                            self._consume(packet)
                    if self.committed != sizes[0] - 1:
                        raise ProtocolError("state_corrupt")
                elif self.db.execute("SELECT count(*) FROM packets").fetchone()[0]:
                    raise ProtocolError("state_corrupt")
            self.resume_seconds = time.perf_counter() - start
            self.write_status()
        except BaseException:
            if self.db:
                self.db.close()
            self.lock.close()
            raise

    def _space(self, required=65536):
        if shutil.disk_usage(self.directory).free < required + RESERVE:
            raise ProtocolError("insufficient_disk")

    def _event(self, reason):
        self.counters[reason] += 1
        return DecodeEvent(reason, self.state.value, self.committed, None)

    def _metadata(self, p):
        self.boot = Bootstrap.from_bytes(p.payload)
        if self.boot.blocks != p.total:
            raise ProtocolError("conflict")
        self.descriptor = self.boot.descriptor
        self.transfer_id = p.transfer_id
        self.state = State.RECEIVING
        self._space(self.descriptor.object_size * 2)

    def _validate(self, p):
        if p.transfer_id != self.transfer_id or p.total != self.boot.blocks:
            raise ProtocolError("conflict")
        block = p.index >> 24
        index = p.index & 0xFFFFFF
        k = self.boot.source_count(block)
        if p.kind not in (1, 2) or index >= k * (
            self.boot.repair_factor if p.kind == 2 else 1
        ):
            raise ProtocolError("fec_symbol_limit")
        if p.kind == 1 and self.boot.mode == "repair-only":
            raise ProtocolError("conflict")

    def _graph(self, block):
        from lt.decode import BlockGraph

        if block not in self.cache:
            if len(self.cache) >= 8:
                self.cache.popitem(last=False)
            graph = BlockGraph(self.boot.source_count(block))
            self.cache[block] = graph
            # Reload on eviction, excluding the new symbol (not yet committed).
            for (raw,) in self.db.execute(
                "SELECT raw FROM packets WHERE kind>0 AND idx>=? AND idx<? ORDER BY kind,idx",
                (block << 24, (block + 1) << 24),
            ):
                self._add(graph, decode(raw))
        self.cache.move_to_end(block)
        return self.cache[block]

    def _add(self, graph, p):
        from lt.sampler import PRNG

        if p.kind == 1:
            nodes = {p.index & 0xFFFFFF}
            value = p.payload
        else:
            prng = PRNG((graph.num_blocks, 0.5, 0.1))
            _, _, nodes = prng.get_src_blocks(seed=int.from_bytes(p.payload[:4], "big"))
            value = p.payload[4:]
        graph.add_block(nodes, int.from_bytes(value, "big"))

    def _consume(self, p):
        block = p.index >> 24
        if block in self.completed:
            return
        graph = self._graph(block)
        self._add(graph, p)
        if len(graph.eliminated) != graph.num_blocks:
            return
        value = b"".join(
            graph.eliminated[i].to_bytes(SYMBOL, "big") for i in range(graph.num_blocks)
        )
        size = self.boot.data_size(block)
        if value[:PREFIX] != self.boot.prefix(block) or any(value[PREFIX + size :]):
            raise ProtocolError("fec_envelope")
        path = self.directory / "blocks" / f"{block:04x}.bin"
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as f:
            f.write(value[PREFIX : PREFIX + size])
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        self.completed.add(block)
        self.received_bytes += size
        self.cache.pop(block, None)
        if len(self.completed) == self.boot.blocks:
            self._assemble()

    def _assemble(self):
        path = self.directory / "object.part"
        with path.open("wb") as out:
            for block in range(self.boot.blocks):
                out.write((self.directory / "blocks" / f"{block:04x}.bin").read_bytes())
            out.flush()
            os.fsync(out.fileno())
        if (
            path.stat().st_size != self.descriptor.object_size
            or sha256_file(path) != self.descriptor.sha256
        ):
            raise ProtocolError("hash")
        path.replace(self.directory / "object.bin")
        self.state = State.VERIFIED

    def feed(self, raw):
        if self.state in (State.ERROR, State.CANCELLED):
            return self._event("inactive")
        try:
            p = decode(raw)
        except ProtocolError as e:
            return self._event(e.reason)
        if self.transfer_id is not None and p.transfer_id != self.transfer_id:
            return self._event("foreign_session")
        if p.kind == 0:
            try:
                boot = Bootstrap.from_bytes(p.payload)
            except ProtocolError as e:
                return self._event(e.reason)
            if self.boot:
                return self._event(
                    "duplicate"
                    if self.boot == boot and p.total == boot.blocks
                    else "conflict"
                )
            self._metadata(p)
            self._space(self.boot.symbols * 8192 + self.descriptor.object_size * 2)
            with self.db:
                self.db.execute("INSERT INTO packets VALUES(?,?,?)", (0, 0, raw))
            pending = list(self.pending.values())
            self.pending.clear()
            for when, value in pending:
                if time.monotonic() - when <= 30:
                    self.feed(value)
            return self._event("metadata")
        if self.boot is None:
            key = (p.transfer_id, p.kind, p.index)
            if key in self.pending:
                return self._event(
                    "duplicate" if self.pending[key][1] == raw else "conflict"
                )
            if (
                len(self.pending) >= 64
                or len({key[0] for key in self.pending} | {p.transfer_id}) > 4
            ):
                return self._event("limit")
            self.pending[key] = (time.monotonic(), raw)
            self.state = State.BUFFERING
            return self._event("buffered")
        try:
            self._validate(p)
        except ProtocolError as e:
            return self._event(e.reason)
        old = self.db.execute(
            "SELECT raw FROM packets WHERE kind=? AND idx=?", (p.kind, p.index)
        ).fetchone()
        if old:
            return self._event("duplicate" if old[0] == raw else "conflict")
        if self.committed >= self.boot.symbols:
            raise ProtocolError("fec_journal_limit")
        self._space()
        if p.index >> 24 not in self.completed:
            self._graph(p.index >> 24)
        with self.db:
            self.db.execute("INSERT INTO packets VALUES(?,?,?)", (p.kind, p.index, raw))
        self.committed += 1
        self.counters["source_symbols" if p.kind == 1 else "repair_symbols"] += 1
        self._consume(p)
        return self._event("accepted")

    def snapshot(self):
        return {
            "schema": 1,
            "storage_schema": "lt1",
            "transport": "aqr2-lt-v1",
            "state": self.state.value,
            "transfer_id": self.transfer_id.hex() if self.transfer_id else None,
            "descriptor": self.descriptor.__dict__ if self.descriptor else None,
            "fec": self.boot.__dict__ if self.boot else None,
            "received_chunks": self.committed,
            "committed_chunks": self.committed,
            "accepted_symbols": self.committed,
            "restored_blocks": len(self.completed),
            "total_blocks": self.boot.blocks if self.boot else None,
            "received_bytes": self.received_bytes,
            "pending_packets": len(self.pending),
            "counters": dict(self.counters),
            "content_verified": self.state == State.VERIFIED,
            "encryption_verified": False,
            "resume_seconds": getattr(self, "resume_seconds", 0),
            "state_bytes": sum(
                p.stat().st_size for p in self.directory.rglob("*") if p.is_file()
            ),
            "missing_ranges": None,
            "progress_note": "Symbols are not independent; no exact remaining-symbol count.",
        }

    def write_status(self):
        tmp = self.directory / "status.tmp"
        tmp.write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.directory / "status.json")

    def cancel(self):
        self.state = State.CANCELLED
        self.pending.clear()

    def close(self):
        try:
            self.write_status()
        finally:
            self.db.close()
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
