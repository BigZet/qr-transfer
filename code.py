from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import struct
import tarfile
import tempfile
import zlib
from pathlib import Path

import qrcode


# ============================================================
# Internal protocol
# ============================================================

MAGIC = b"AQR1"

TYPE_META = 0
TYPE_DATA = 1

HEADER = struct.Struct(">4sB8sHHHI")

# Консервативный размер блока.
# Version 40-L допускает около 2953 байт бинарных данных,
# но часть места занимает наш служебный заголовок.
DEFAULT_CHUNK_SIZE = 2800


# ============================================================
# Packet
# ============================================================

def make_packet(
    packet_type: int,
    file_id: bytes,
    index: int,
    total: int,
    payload: bytes,
) -> bytes:

    crc = zlib.crc32(payload) & 0xFFFFFFFF

    return HEADER.pack(
        MAGIC,
        packet_type,
        file_id,
        index,
        total,
        len(payload),
        crc,
    ) + payload


# ============================================================
# Frame generation
# ============================================================

def make_frame(
    payload: bytes,
    box_size: int,
) -> bytes:

    code = qrcode.QRCode(
        version=40,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=box_size,
        border=4,
    )

    code.add_data(
        payload,
        optimize=0,
    )

    try:
        code.make(fit=False)

    except qrcode.exceptions.DataOverflowError:
        raise RuntimeError(
            "Блок данных слишком большой. "
            "Уменьши --chunk-size."
        )

    image = code.make_image(
        fill_color="black",
        back_color="white",
    ).convert("RGB")

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="PNG",
        optimize=True,
    )

    return buffer.getvalue()


# ============================================================
# Folder -> tar.xz
# ============================================================

EXCLUDED_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ipynb_checkpoints",
}


def archive_filter(info: tarfile.TarInfo):
    parts = Path(info.name).parts

    if any(part in EXCLUDED_NAMES for part in parts):
        return None

    return info


def archive_folder(
    source: Path,
    destination: Path,
):
    with tarfile.open(
        destination,
        mode="w:xz",
        preset=9,
    ) as archive:

        archive.add(
            source,
            arcname=source.name,
            recursive=True,
            filter=archive_filter,
        )


# ============================================================
# HTML
# ============================================================

def create_html(
    frames: list[bytes],
    output: Path,
    interval_ms: int,
):
    encoded_frames = []

    for frame in frames:
        encoded = base64.b64encode(
            frame
        ).decode("ascii")

        encoded_frames.append(
            "data:image/png;base64," + encoded
        )

    frames_json = json.dumps(
        encoded_frames,
        separators=(",", ":"),
    )

    html = """
<!DOCTYPE html>
<html>

<head>

<meta charset="UTF-8">

<title>File</title>

<style>

html,
body {
    margin: 0;
    padding: 0;

    width: 100%;
    height: 100%;

    overflow: hidden;
    background: white;

    font-family:
        Menlo,
        Monaco,
        Consolas,
        monospace;
}

#screen {
    width: 100%;
    height: 100%;

    display: flex;

    align-items: center;
    justify-content: center;

    background: white;
}

#image {
    width: 90vmin;
    height: 90vmin;

    object-fit: contain;

    image-rendering: pixelated;
    image-rendering: crisp-edges;
}

#status {
    position: fixed;

    left: 12px;
    bottom: 10px;

    padding: 5px 9px;

    background: rgba(255, 255, 255, 0.95);

    color: #222;

    font-size: 13px;
}

#help {
    position: fixed;

    left: 12px;
    top: 10px;

    padding: 5px 9px;

    background: rgba(255, 255, 255, 0.95);

    color: #555;

    font-size: 12px;
}

</style>

</head>

<body>

<div id="screen">
    <img id="image">
</div>

<div id="help">
SPACE pause &nbsp; | &nbsp;
← → frame &nbsp; | &nbsp;
+ faster &nbsp; | &nbsp;
- slower
</div>

<div id="status"></div>

<script>

const frames = __FRAMES__;

let index = 0;
let interval = __INTERVAL__;
let running = true;
let timer = null;

const image =
    document.getElementById("image");

const status =
    document.getElementById("status");


for (const src of frames) {

    const preload =
        new Image();

    preload.src = src;
}


function render() {

    image.src =
        frames[index];

    status.textContent =
        (index + 1)
        + " / "
        + frames.length
        + "   |   "
        + interval
        + " ms";
}


function nextFrame() {

    index =
        (index + 1)
        % frames.length;

    render();
}


function previousFrame() {

    index--;

    if (index < 0) {
        index =
            frames.length - 1;
    }

    render();
}


function restartTimer() {

    if (timer !== null) {
        clearInterval(timer);
    }

    timer = null;

    if (running) {

        timer =
            setInterval(
                nextFrame,
                interval
            );
    }
}


document.addEventListener(
    "keydown",

    event => {

        if (
            event.code === "Space"
        ) {

            event.preventDefault();

            running =
                !running;

            restartTimer();
        }

        else if (
            event.key === "+"
            ||
            event.key === "="
        ) {

            interval =
                Math.max(
                    150,
                    interval - 50
                );

            restartTimer();
            render();
        }

        else if (
            event.key === "-"
        ) {

            interval += 50;

            restartTimer();
            render();
        }

        else if (
            event.key === "ArrowRight"
        ) {

            nextFrame();
        }

        else if (
            event.key === "ArrowLeft"
        ) {

            previousFrame();
        }
    }
);


render();
restartTimer();

</script>

</body>

</html>
"""

    html = html.replace(
        "__FRAMES__",
        frames_json,
    )

    html = html.replace(
        "__INTERVAL__",
        str(interval_ms),
    )

    output.write_text(
        html,
        encoding="utf-8",
    )

# ============================================================
# Encoding
# ============================================================

def encode(
    source: Path,
    output: Path,
    interval_ms: int,
    box_size: int,
    chunk_size: int,
):

    temporary_directory = None

    # --------------------------------------------------------
    # Folder
    # --------------------------------------------------------

    if source.is_dir():

        temporary_directory = (
            tempfile.TemporaryDirectory()
        )

        archive_path = (
            Path(temporary_directory.name)
            / f"{source.name}.tar.xz"
        )

        print(
            f"Подготовка: {source}"
        )

        archive_folder(
            source,
            archive_path,
        )

        input_path = archive_path

        transmitted_name = (
            f"{source.name}.tar.xz"
        )

    # --------------------------------------------------------
    # File
    # --------------------------------------------------------

    elif source.is_file():

        input_path = source

        transmitted_name = source.name

    else:

        raise FileNotFoundError(
            f"Не найдено: {source.resolve()}"
        )

    # --------------------------------------------------------
    # Read
    # --------------------------------------------------------

    data = input_path.read_bytes()

    if not data:
        raise RuntimeError(
            "Исходные данные пустые."
        )

    sha256 = hashlib.sha256(
        data
    ).hexdigest()

    file_id = bytes.fromhex(
        sha256[:16]
    )

    total = math.ceil(
        len(data) / chunk_size
    )

    if total > 65535:
        raise RuntimeError(
            "Слишком большой объём данных."
        )

    print()
    print(
        "Размер:",
        f"{len(data):,}",
        "байт",
    )

    print(
        "Частей:",
        total,
    )

    print(
        "Размер блока:",
        chunk_size,
        "байт",
    )

    print(
        "SHA256:",
        sha256,
    )

    # --------------------------------------------------------
    # Metadata
    # --------------------------------------------------------

    metadata = {
        "filename": transmitted_name,
        "size": len(data),
        "sha256": sha256,
        "protocol": "AQR1",
    }

    metadata_bytes = json.dumps(
        metadata,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")

    metadata_packet = make_packet(
        TYPE_META,
        file_id,
        0,
        total,
        metadata_bytes,
    )

    metadata_frame = make_frame(
        metadata_packet,
        box_size,
    )

    # --------------------------------------------------------
    # Data frames
    # --------------------------------------------------------

    data_frames = []

    print()

    for i in range(total):

        start = (
            i * chunk_size
        )

        end = (
            start + chunk_size
        )

        chunk = data[start:end]

        packet = make_packet(
            TYPE_DATA,
            file_id,
            i + 1,
            total,
            chunk,
        )

        frame = make_frame(
            packet,
            box_size,
        )

        data_frames.append(
            frame
        )

        print(
            f"{i + 1:03d}/{total:03d} "
            f"{len(chunk):4d} bytes"
        )

    # --------------------------------------------------------
    # Playback
    #
    # Служебный кадр повторяется периодически,
    # чтобы принимающая сторона могла подключиться
    # в любой момент.
    # --------------------------------------------------------

    playback = [
        metadata_frame
    ]

    for i, frame in enumerate(
        data_frames,
        start=1,
    ):

        playback.append(
            frame
        )

        if (
            i % 8 == 0
            and i != total
        ):
            playback.append(
                metadata_frame
            )

    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    create_html(
        frames=playback,
        output=output,
        interval_ms=interval_ms,
    )

    if not output.exists():
        raise RuntimeError(
            "Файл не был создан."
        )

    if output.stat().st_size == 0:
        raise RuntimeError(
            "Создан пустой файл."
        )

    print()
    print("=" * 50)

    print(
        "Готово:",
        output.resolve(),
    )

    print(
        "Размер:",
        f"{output.stat().st_size:,}",
        "байт",
    )

    print(
        "Кадров:",
        len(playback),
    )

    print(
        "Интервал:",
        interval_ms,
        "ms",
    )

    print(
        "Цикл:",
        f"{len(playback) * interval_ms / 1000:.1f}",
        "сек",
    )

    print("=" * 50)

    if temporary_directory is not None:
        temporary_directory.cleanup()


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="File utility."
    )

    parser.add_argument(
        "source",
        type=Path,
        help="Файл или папка.",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Выходной HTML-файл.",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=600,
        help="Интервал кадров, мс.",
    )

    parser.add_argument(
        "--box-size",
        type=int,
        default=6,
        help="Размер элемента изображения.",
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Размер блока данных.",
    )

    args = parser.parse_args()

    source = (
        args.source
        .expanduser()
        .resolve()
    )

    if args.output is None:

        output = Path(
            f"{source.stem}_file.html"
        )

    else:

        output = (
            args.output
            .expanduser()
            .resolve()
        )

    if args.interval < 150:
        raise ValueError(
            "--interval должен быть >= 150"
        )

    if not (
        100 <= args.chunk_size <= 2850
    ):
        raise ValueError(
            "--chunk-size должен быть "
            "от 100 до 2850."
        )

    encode(
        source=source,
        output=output,
        interval_ms=args.interval,
        box_size=args.box_size,
        chunk_size=args.chunk_size,
    )


if __name__ == "__main__":
    main()