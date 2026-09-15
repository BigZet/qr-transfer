"""Normative AQR2-repeat v1 wire format; see docs/specs/aqr2.md."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import struct
import zlib

HEADER = struct.Struct(">4sBBHHH16sIIII")
MAX_PACKET = 2953  # QR V40-L, byte mode (includes our header).
MAX_PAYLOAD = MAX_PACKET - HEADER.size
MAX_METADATA = 1024
META, DATA = 0, 1
REPEAT_PROFILE = 1
DESCRIPTOR_VERSION = 1
UINT32_MAX = 0xFFFFFFFF


class ProtocolError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Limits:
    max_object: int = 64 * 1024 * 1024
    max_chunks: int = 65536
    pending_packets: int = 64
    pending_sessions: int = 4
    pending_seconds: float = 30.0

    def __post_init__(self):
        if any(type(x) is not int or x < 1 for x in (
            self.max_object, self.max_chunks, self.pending_packets, self.pending_sessions
        )) or not 0 < self.pending_seconds <= 3600:
            raise ValueError("Invalid receiver limits")


@dataclass(frozen=True)
class Packet:
    kind: int
    transfer_id: bytes
    index: int
    total: int
    payload: bytes


def encode(packet: Packet) -> bytes:
    if len(packet.transfer_id) != 16:
        raise ProtocolError("format")
    try:
        header = HEADER.pack(b"AQR2", packet.kind, 0, HEADER.size, REPEAT_PROFILE,
                             DESCRIPTOR_VERSION, packet.transfer_id, packet.index,
                             packet.total, len(packet.payload), 0)
    except (struct.error, TypeError, OverflowError) as error:
        raise ProtocolError("format") from error
    raw = header + packet.payload
    crc = zlib.crc32(raw) & UINT32_MAX
    raw = raw[:40] + struct.pack(">I", crc) + raw[44:]
    decode(raw)
    return raw


def decode(raw: bytes) -> Packet:
    if len(raw) > MAX_PACKET:
        raise ProtocolError("limit")
    if len(raw) < HEADER.size:
        raise ProtocolError("length")
    magic, kind, flags, hlen, profile, version, tid, index, total, length, crc = HEADER.unpack_from(raw)
    if magic != b"AQR2":
        raise ProtocolError("format")
    if hlen != HEADER.size or len(raw) != hlen + length:
        raise ProtocolError("length")
    if zlib.crc32(raw[:40] + b"\0" * 4 + raw[44:]) & UINT32_MAX != crc:
        raise ProtocolError("crc")
    if flags or kind not in (META, DATA) or profile != REPEAT_PROFILE or version != DESCRIPTOR_VERSION:
        raise ProtocolError("unsupported")
    if not total or (kind == DATA and (index >= total or not length)) or (kind == META and index != 0):
        raise ProtocolError("format")
    if kind == META and length > MAX_METADATA:
        raise ProtocolError("limit")
    return Packet(kind, tid, index, total, raw[HEADER.size:])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("format")
        result[key] = value
    return result


@dataclass(frozen=True)
class TransferDescriptor:
    object_size: int
    sha256: str
    chunk_size: int
    total: int
    container: str = "opaque-test"
    descriptor_version: int = 1
    transport: str = "aqr2-repeat"

    def validate(self, limits: Limits = Limits()) -> None:
        if any(type(x) is not int for x in (self.object_size, self.chunk_size, self.total, self.descriptor_version)):
            raise ProtocolError("format")
        if self.descriptor_version != 1 or self.transport != "aqr2-repeat" or self.container not in ("opaque-test", "7z-aes256"):
            raise ProtocolError("unsupported")
        if not 1 <= self.object_size <= limits.max_object or not 1 <= self.total <= min(UINT32_MAX, limits.max_chunks):
            raise ProtocolError("limit")
        if not 1 <= self.chunk_size <= MAX_PAYLOAD:
            raise ProtocolError("limit")
        if self.total != (self.object_size + self.chunk_size - 1) // self.chunk_size:
            raise ProtocolError("format")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ProtocolError("format")

    def to_bytes(self, limits: Limits = Limits()) -> bytes:
        self.validate(limits)
        result = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(result) > MAX_METADATA:
            raise ProtocolError("limit")
        return result

    @classmethod
    def from_bytes(cls, raw: bytes, limits: Limits = Limits()) -> TransferDescriptor:
        if len(raw) > MAX_METADATA:
            raise ProtocolError("limit")
        try:
            obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(obj, dict) or set(obj) != set(cls.__dataclass_fields__):
                raise ProtocolError("format")
            result = cls(**obj)
            result.validate(limits)
            return result
        except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as error:
            raise ProtocolError("format") from error


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
