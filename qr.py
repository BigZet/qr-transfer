from __future__ import annotations

import argparse
import hashlib
import json
import struct
import time
import zlib
from pathlib import Path

import mss
from PIL import Image
import zxingcpp


# ============================================================
# Должно совпадать с encode script
# ============================================================

MAGIC = b"AQR1"

TYPE_META = 0
TYPE_DATA = 1

HEADER = struct.Struct(">4sB8sHHHI")


def parse_packet(raw: bytes):
    """
    Разбирает один QR packet.

    Формат:
        magic       4 bytes
        type        1 byte
        file_id     8 bytes
        index       2 bytes
        total       2 bytes
        payload_len 2 bytes
        crc32       4 bytes
        payload     N bytes
    """

    if len(raw) < HEADER.size:
        return None

    try:
        (
            magic,
            packet_type,
            file_id,
            index,
            total,
            payload_len,
            expected_crc,
        ) = HEADER.unpack(raw[:HEADER.size])

    except struct.error:
        return None

    if magic != MAGIC:
        return None

    payload = raw[HEADER.size:]

    if len(payload) != payload_len:
        return None

    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF

    if actual_crc != expected_crc:
        print(
            f"CRC ERROR: "
            f"chunk={index}, "
            f"expected={expected_crc:08x}, "
            f"actual={actual_crc:08x}"
        )
        return None

    return {
        "type": packet_type,
        "file_id": file_id,
        "index": index,
        "total": total,
        "payload": payload,
    }


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--monitor",
        type=int,
        default=1,
        help="Номер монитора для захвата",
    )

    parser.add_argument(
        "--fps",
        type=float,
        default=12,
        help="Сколько screenshot в секунду анализировать",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Максимальное время ожидания",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
    )

    args = parser.parse_args()

    metadata = None
    chunks = {}

    active_file_id = None
    expected_total = None

    started = time.monotonic()

    last_seen_index = None

    with mss.mss() as sct:

        print("Мониторы:")
        print()

        for i, monitor in enumerate(sct.monitors):

            print(
                f"{i}: "
                f"{monitor['width']}x{monitor['height']} "
                f"left={monitor['left']} "
                f"top={monitor['top']}"
            )

        print()

        if args.monitor >= len(sct.monitors):
            raise ValueError(
                f"Монитор {args.monitor} не существует"
            )

        monitor = sct.monitors[args.monitor]

        print(f"Используется монитор: {args.monitor}")
        print("Ожидаю animated QR...")
        print()

        while True:

            elapsed = time.monotonic() - started

            if elapsed > args.timeout:
                print()
                print("TIMEOUT")

                if expected_total is not None:

                    missing = [
                        i
                        for i in range(1, expected_total + 1)
                        if i not in chunks
                    ]

                    print(
                        f"Получено: "
                        f"{len(chunks)}/{expected_total}"
                    )

                    print("Не хватает:")
                    print(missing)

                return

            # =================================================
            # Screenshot
            # =================================================

            shot = sct.grab(monitor)

            image = Image.frombytes(
                "RGB",
                shot.size,
                shot.rgb,
            )

            # =================================================
            # Decode QR
            # =================================================

            result = zxingcpp.read_barcode(image)

            if result is None:
                time.sleep(1 / args.fps)
                continue

            raw = bytes(result.bytes)

            packet = parse_packet(raw)

            if packet is None:
                time.sleep(1 / args.fps)
                continue

            file_id = packet["file_id"]

            # =================================================
            # Выбираем поток
            # =================================================

            if active_file_id is None:

                active_file_id = file_id

                print(
                    "Поток обнаружен:",
                    active_file_id.hex()
                )

                print()

            if file_id != active_file_id:
                continue

            expected_total = packet["total"]

            # =================================================
            # Metadata
            # =================================================

            if packet["type"] == TYPE_META:

                try:

                    new_metadata = json.loads(
                        packet["payload"].decode("utf-8")
                    )

                except Exception as e:

                    print(
                        "Ошибка metadata:",
                        e
                    )

                    continue

                if metadata is None:

                    metadata = new_metadata

                    print("Metadata:")
                    print(
                        "  filename:",
                        metadata.get("filename")
                    )

                    print(
                        "  size:",
                        metadata.get("size"),
                        "bytes"
                    )

                    print(
                        "  sha256:",
                        metadata.get("sha256")
                    )

                    print(
                        "  chunks:",
                        expected_total
                    )

                    print()

            # =================================================
            # Data
            # =================================================

            elif packet["type"] == TYPE_DATA:

                index = packet["index"]

                if not (
                    1 <= index <= expected_total
                ):
                    continue

                # Уже получали этот QR
                if index in chunks:

                    time.sleep(1 / args.fps)
                    continue

                chunks[index] = packet["payload"]

                last_seen_index = index

                print(
                    f"[{len(chunks):03d}/"
                    f"{expected_total:03d}] "
                    f"chunk {index:03d} "
                    f"({len(packet['payload'])} bytes)"
                )

            # =================================================
            # Все chunks получены?
            # =================================================

            if (
                metadata is not None
                and expected_total is not None
                and len(chunks) == expected_total
            ):

                missing = [
                    i
                    for i in range(
                        1,
                        expected_total + 1
                    )
                    if i not in chunks
                ]

                if missing:
                    continue

                print()
                print("Все chunks получены.")
                print("Собираю файл...")

                file_data = b"".join(
                    chunks[i]
                    for i in range(
                        1,
                        expected_total + 1
                    )
                )

                # =================================================
                # Размер
                # =================================================

                expected_size = metadata["size"]

                print()
                print(
                    "Полученный размер:",
                    len(file_data)
                )

                print(
                    "Ожидаемый размер:",
                    expected_size
                )

                if len(file_data) != expected_size:
                    raise RuntimeError(
                        "Размер файла не совпадает"
                    )

                # =================================================
                # SHA256
                # =================================================

                actual_sha256 = hashlib.sha256(
                    file_data
                ).hexdigest()

                expected_sha256 = metadata["sha256"]

                print()
                print("Expected SHA256:")
                print(expected_sha256)

                print()
                print("Actual SHA256:")
                print(actual_sha256)

                if actual_sha256 != expected_sha256:
                    raise RuntimeError(
                        "SHA256 не совпадает"
                    )

                # =================================================
                # Save
                # =================================================

                filename = Path(
                    metadata["filename"]
                ).name

                args.output_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                output_path = (
                    args.output_dir / filename
                )

                output_path.write_bytes(
                    file_data
                )

                elapsed = (
                    time.monotonic()
                    - started
                )

                print()
                print("=" * 60)
                print("ФАЙЛ УСПЕШНО ВОССТАНОВЛЕН")
                print("=" * 60)

                print(
                    "Путь:",
                    output_path.resolve()
                )

                print(
                    "Размер:",
                    len(file_data),
                    "bytes"
                )

                print(
                    "Время:",
                    f"{elapsed:.1f}",
                    "sec"
                )

                return

            time.sleep(1 / args.fps)


if __name__ == "__main__":
    main()