"""Explicit AQR1 compatibility. Payload is NOT encrypted by this protocol."""
from __future__ import annotations

import struct
import zlib
import hashlib
import json

from .protocol import ProtocolError

HEADER = struct.Struct(">4sB8sHHHI")


def make_packet(packet_type: int, file_id: bytes, index: int, total: int, payload: bytes) -> bytes:
    if len(file_id) != 8 or packet_type not in (0, 1):
        raise ProtocolError("legacy_format")
    try:
        return HEADER.pack(b"AQR1", packet_type, file_id, index, total, len(payload),
                           zlib.crc32(payload) & 0xFFFFFFFF) + payload
    except struct.error as error:
        raise ProtocolError("legacy_format") from error


def parse_packet(raw: bytes) -> dict:
    if len(raw) < HEADER.size or len(raw) > HEADER.size + 65535:
        raise ProtocolError("legacy_length")
    magic, kind, tid, index, total, size, crc = HEADER.unpack_from(raw)
    if magic != b"AQR1" or kind not in (0, 1):
        raise ProtocolError("legacy_format")
    payload = raw[HEADER.size:]
    if len(payload) != size:
        raise ProtocolError("legacy_length")
    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
        raise ProtocolError("legacy_crc")
    return {"type": kind, "file_id": tid, "index": index, "total": total, "payload": payload}


def assemble(sequence, *, max_bytes: int = 64 * 1024 * 1024, max_chunks: int = 65535) -> bytes:
    """Bounded legacy test adapter. Returns bytes, ignores filename, never extracts.

    Requires metadata first (unlike AQR2's bounded late-join buffering).
    Original screen receiver remains available as qr.py.
    """
    metadata, tid, total = None, None, None
    chunks = {}
    received_bytes = 0
    for raw in sequence:
        packet = parse_packet(raw)
        if metadata is None:
            if packet["type"] != 0:
                continue
            try:
                candidate = json.loads(packet["payload"])
                if not isinstance(candidate, dict) or candidate.get("protocol") != "AQR1":
                    raise ProtocolError("legacy_format")
                size = candidate.get("size")
                digest = candidate.get("sha256")
                if type(size) is not int or not 0 <= size <= max_bytes or not 1 <= packet["total"] <= max_chunks:
                    raise ProtocolError("limit")
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ProtocolError("legacy_format")
                metadata, tid, total = candidate, packet["file_id"], packet["total"]
            except (ValueError, UnicodeError) as error:
                raise ProtocolError("legacy_format") from error
        elif packet["file_id"] != tid:
            continue
        elif packet["total"] != total:
            raise ProtocolError("conflict")
        elif packet["type"] == 0:
            if json.loads(packet["payload"]) != metadata:
                raise ProtocolError("conflict")
        else:
            index, payload = packet["index"], packet["payload"]
            if index >= total:
                raise ProtocolError("legacy_format")
            if index in chunks:
                if chunks[index] != payload:
                    raise ProtocolError("conflict")
                continue
            if received_bytes + len(payload) > metadata["size"]:
                raise ProtocolError("limit")
            chunks[index] = payload
            received_bytes += len(payload)
    if metadata is None or len(chunks) != total:
        raise ProtocolError("incomplete")
    result = b"".join(chunks[index] for index in range(total))
    if len(result) != metadata["size"] or hashlib.sha256(result).hexdigest() != metadata["sha256"]:
        raise ProtocolError("hash")
    return result
