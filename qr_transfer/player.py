"""Pre-generated packed QR matrices; browser never receives a password."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import struct
import time
import zlib
import uuid

from .container import verify_object
from .protocol import TransferDescriptor, MAX_PACKET
from .transfer import prepare_object, packets

SIDE, BORDER = 177, 4
FRAME_BYTES = (SIDE * SIDE + 7) // 8
HEADER = struct.Struct(">4sHHI")
MAX_MATRICES = 32 << 20
MAX_STANDALONE = 8 << 20
MAX_PAGED = 256 << 20
ASSETS = Path(__file__).parent / "assets"


def partition_matrices(path: Path, *, frames_per_part=256, expected_crc=None):
    """Independent CRC-protected matrix pages; no HTTP Range dependency."""
    if type(frames_per_part) is not int or not 8 <= frames_per_part <= 1024:
        raise ValueError("part-frames must be between 8 and 1024")
    with path.open("rb") as source:
        header = source.read(HEADER.size)
        if len(header) != HEADER.size:
            raise ValueError("Invalid matrix header")
        magic, side, border, count = HEADER.unpack(header)
        if (magic, side, border) != (b"QRM1", SIDE, BORDER) or count < 2:
            raise ValueError("Invalid matrix header")
        if path.stat().st_size != HEADER.size + count * FRAME_BYTES or path.stat().st_size > MAX_PAGED:
            raise ValueError("Invalid matrix size")
        directory = path.parent / ("parts-" + uuid.uuid4().hex)
        directory.mkdir()
        crc, items = zlib.crc32(header), []
        remaining = count
        while remaining:
            length = min(remaining, frames_per_part) * FRAME_BYTES
            block = source.read(length)
            if len(block) != length:
                raise ValueError("Truncated matrix file")
            (directory / f"{len(items):06d}.bin").write_bytes(block)
            items.append({"bytes": length, "crc32": zlib.crc32(block) & 0xffffffff})
            crc = zlib.crc32(block, crc)
            remaining -= length // FRAME_BYTES
        if source.read(1) or (expected_crc is not None and crc & 0xffffffff != expected_crc):
            raise ValueError("Matrix checksum mismatch")
    return {"schema": 1, "directory": directory.name, "frames_per_part": frames_per_part, "items": items}


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


def estimate(descriptor: TransferDescriptor, *, metadata_every=8, interval_ms=600, slots=1, update_mode="sync", transport="repeat", repair_factor=3, fec_mode="systematic", visual="mono", part_frames=256):
    descriptor.validate()
    from .rgb import layers_for
    layers = layers_for(visual)
    if type(part_frames) is not int or not 8 <= part_frames <= 1024:
        raise ValueError("part-frames must be between 8 and 1024")
    if slots not in (1,2) or update_mode not in ("sync", "staggered"):
        raise ValueError("Invalid layout")
    if type(metadata_every) is not int or not 1 <= metadata_every <= 1024 or not 50 <= interval_ms <= 10000:
        raise ValueError("Invalid player timing")
    data_count = descriptor.total
    if transport == "lt":
        from .fec import bootstrap
        data_count = bootstrap(descriptor, repair_factor, fec_mode).symbols
    elif transport != "repeat":
        raise ValueError("Unknown transport")
    count = data_count + 1
    size = HEADER.size + count * FRAME_BYTES
    from .rgb import schedule
    schedule_frames = len(schedule(data_count, metadata_every, layers, slots))
    return {"visual": visual, "layers": layers, "slots": slots, "update_mode": update_mode, "unique_frames": count, "matrix_bytes": size, "base64_bytes": ((size + 2) // 3) * 4,
            "cycle_frames": schedule_frames, "cycle_seconds": schedule_frames * interval_ms / (1000 * slots),
            "metadata_fraction": (schedule_frames * layers - data_count) / (schedule_frames * layers),
            "part_frames": part_frames, "parts": (count + part_frames - 1) // part_frames,
            "matrix_buffer_limit_bytes": 5 * part_frames * FRAME_BYTES + FRAME_BYTES,
            "external_supported": size <= MAX_PAGED, "standalone_supported": size <= MAX_STANDALONE}


def render(archive: Path, descriptor: TransferDescriptor, output: Path, *, generator="segno",
           metadata_every=8, interval_ms=600, standalone=True, progress=None, slots=1, update_mode="sync", transport="repeat", repair_factor=3, fec_mode="systematic", visual="mono", part_frames=256):
    verify_object(archive, descriptor)
    budget = estimate(descriptor, metadata_every=metadata_every, interval_ms=interval_ms, slots=slots, update_mode=update_mode, transport=transport, repair_factor=repair_factor, fec_mode=fec_mode, visual=visual, part_frames=part_frames)
    if not budget["external_supported"] or (standalone and not budget["standalone_supported"]):
        raise ValueError("Player size limit; use external-only or a smaller object")
    if output.absolute().is_relative_to(archive.absolute()) or archive.absolute().is_relative_to(output.absolute()):
        raise ValueError("Output must be separate")
    output.mkdir(parents=True, exist_ok=False)
    transfer = prepare_object(archive, chunk_size=descriptor.chunk_size, container="7z-aes256")
    if transport == "lt":
        from .fec import bootstrap, pool
        sequence = pool(archive, bootstrap(descriptor, repair_factor, fec_mode), transfer.transfer_id)
    else:
        sequence = packets(transfer, metadata_every=descriptor.total+1)
    started = time.perf_counter()
    try:
        path = output / "frames.bin"
        crc = 0
        with path.open("xb") as target:
            header = HEADER.pack(b"QRM1", SIDE, BORDER, budget["unique_frames"])
            target.write(header)
            crc = zlib.crc32(header)
            # Store metadata once; schedule repeats references in JS.
            for index, raw in enumerate(sequence):
                frame = pack_matrix(matrix(raw, generator))
                target.write(frame)
                crc = zlib.crc32(frame, crc)
                if progress and (index % 16 == 0 or index + 1 == budget["unique_frames"]):
                    progress(index + 1, budget["unique_frames"])
        if path.stat().st_size != budget["matrix_bytes"]:
            raise ValueError("Frame count mismatch")
        parts = partition_matrices(path, frames_per_part=part_frames, expected_crc=crc & 0xffffffff)
        from .rgb import SERVICE
        calibration = base64.b64encode(pack_matrix(matrix(SERVICE, generator))).decode("ascii")
        config = {"schema": 1, "profile": visual, "visual": visual, "calibration_matrix": calibration, "transfer_id": transfer.transfer_id.hex(),
                  "descriptor": transfer.descriptor.__dict__, "data_frames": budget["unique_frames"]-1, "transport":transport, "interval_ms": interval_ms,
                  "slots": slots, "update_mode": update_mode, "metadata_every": metadata_every, "matrix_bytes": budget["matrix_bytes"],
                  "matrix_crc32": crc & 0xffffffff, "parts": parts}
        template = (ASSETS / "player.html").read_text(encoding="utf-8")
        config_text = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")
        template = template.replace("__CONFIG__", config_text)
        js = (ASSETS / "player.js").read_text(encoding="utf-8")
        (output / "player.js").write_text(js, encoding="utf-8")
        template = template.replace('<script src="player.js"></script>', "<script>\n" + js.replace("</", "<\\/") + "\n</script>")
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
        result = {"schema": 1, "transport":transport, "repair_factor":repair_factor if transport=="lt" else None, "fec_mode":fec_mode if transport=="lt" else None, "generator": generator, "profile": visual, **budget,
                  "prepare_seconds": time.perf_counter() - started,
                  "standalone_created": standalone, "transfer_id": transfer.transfer_id.hex()}
        (output / "prepare.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    except BaseException:
        # No readiness marker on partial generation; outputs are never reused.
        for name in ("index.html", "standalone.html"):
            (output / name).unlink(missing_ok=True)
        raise
