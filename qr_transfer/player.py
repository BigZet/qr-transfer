"""Pre-generated packed QR matrices; browser never receives a password."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import struct
import time
import zlib

from .container import verify_object
from .protocol import TransferDescriptor, MAX_PACKET
from .transfer import prepare_object, packets

SIDE, BORDER = 177, 4
FRAME_BYTES = (SIDE * SIDE + 7) // 8
HEADER = struct.Struct(">4sHHI")
MAX_MATRICES = 32 << 20
MAX_STANDALONE = 8 << 20
ASSETS = Path(__file__).parent / "assets"


def matrix(raw: bytes, generator="segno"):
    if not 1 <= len(raw) <= MAX_PACKET:
        raise ValueError("Packet does not fit QR V40-L")
    if generator == "segno":
        import segno
        return segno.make(raw, version=40, error="L", mode="byte", micro=False, boost_error=False).matrix
    if generator == "qrcode":
        import qrcode
        from qrcode.util import QRData, MODE_8BIT_BYTE
        qr = qrcode.QRCode(version=40, error_correction=qrcode.constants.ERROR_CORRECT_L, border=4)
        qr.add_data(QRData(raw, mode=MODE_8BIT_BYTE), optimize=0)
        qr.make(fit=False)
        return qr.modules
    raise ValueError("Unknown QR generator")


def pack_matrix(rows) -> bytes:
    if len(rows) != SIDE or any(len(row) != SIDE for row in rows):
        raise ValueError("Unexpected QR dimensions")
    packed = bytearray(FRAME_BYTES)
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            if value:
                bit = y * SIDE + x
                packed[bit >> 3] |= 0x80 >> (bit & 7)
    return bytes(packed)


def estimate(descriptor: TransferDescriptor, *, metadata_every=8, interval_ms=600):
    descriptor.validate()
    if type(metadata_every) is not int or not 1 <= metadata_every <= 1024 or not 50 <= interval_ms <= 10000:
        raise ValueError("Invalid player timing")
    count = descriptor.total + 1
    size = HEADER.size + count * FRAME_BYTES
    schedule_frames = descriptor.total + 1 + descriptor.total // metadata_every
    return {"unique_frames": count, "matrix_bytes": size, "base64_bytes": ((size + 2) // 3) * 4,
            "cycle_frames": schedule_frames, "cycle_seconds": schedule_frames * interval_ms / 1000,
            "metadata_fraction": (schedule_frames - descriptor.total) / schedule_frames,
            "external_supported": size <= MAX_MATRICES, "standalone_supported": size <= MAX_STANDALONE}


def render(archive: Path, descriptor: TransferDescriptor, output: Path, *, generator="segno",
           metadata_every=8, interval_ms=600, standalone=True, progress=None):
    verify_object(archive, descriptor)
    budget = estimate(descriptor, metadata_every=metadata_every, interval_ms=interval_ms)
    if not budget["external_supported"] or (standalone and not budget["standalone_supported"]):
        raise ValueError("Player size limit; use external-only or a smaller object")
    if output.absolute().is_relative_to(archive.absolute()) or archive.absolute().is_relative_to(output.absolute()):
        raise ValueError("Output must be separate")
    output.mkdir(parents=True, exist_ok=False)
    transfer = prepare_object(archive, chunk_size=descriptor.chunk_size, container="7z-aes256")
    started = time.perf_counter()
    try:
        path = output / "frames.bin"
        crc = 0
        with path.open("xb") as target:
            header = HEADER.pack(b"QRM1", SIDE, BORDER, budget["unique_frames"])
            target.write(header)
            crc = zlib.crc32(header)
            # Store metadata once; schedule repeats references in JS.
            for index, raw in enumerate(packets(transfer, metadata_every=descriptor.total + 1)):
                frame = pack_matrix(matrix(raw, generator))
                target.write(frame)
                crc = zlib.crc32(frame, crc)
                if progress and (index % 16 == 0 or index + 1 == budget["unique_frames"]):
                    progress(index + 1, budget["unique_frames"])
        if path.stat().st_size != budget["matrix_bytes"]:
            raise ValueError("Frame count mismatch")
        config = {"schema": 1, "profile": "mono-safe", "transfer_id": transfer.transfer_id.hex(),
                  "descriptor": transfer.descriptor.__dict__, "interval_ms": interval_ms,
                  "metadata_every": metadata_every, "matrix_bytes": budget["matrix_bytes"],
                  "matrix_crc32": crc & 0xffffffff}
        template = (ASSETS / "player.html").read_text(encoding="utf-8")
        config_text = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")
        template = template.replace("__CONFIG__", config_text)
        js = (ASSETS / "player.js").read_text(encoding="utf-8")
        (output / "player.js").write_text(js, encoding="utf-8")
        (output / "index.html").write_text(template.replace("__DATA__", ""), encoding="utf-8")
        if standalone:
            # Stream base64 into HTML; don't build a second complete HTML string.
            before, after = template.split("__DATA__")
            after = after.replace('<script src="player.js"></script>', "<script>\n" + js.replace("</", "<\\/") + "\n</script>")
            with (output / "standalone.html").open("w", encoding="utf-8") as target, path.open("rb") as source:
                target.write(before)
                for block in iter(lambda: source.read(3 * 16384), b""):
                    target.write(base64.b64encode(block).decode("ascii"))
                target.write(after)
        result = {"schema": 1, "generator": generator, "profile": "mono-safe", **budget,
                  "prepare_seconds": time.perf_counter() - started,
                  "standalone_created": standalone, "transfer_id": transfer.transfer_id.hex()}
        (output / "prepare.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    except BaseException:
        # No readiness marker on partial generation; outputs are never reused.
        for name in ("index.html", "standalone.html"):
            (output / name).unlink(missing_ok=True)
        raise
