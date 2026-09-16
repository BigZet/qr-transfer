"""Update generated index.html without regenerating matrices or changing transfer ID."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import tempfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_transfer.player import ASSETS, partition_matrices


class ConfigParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "config":
            self.inside = True
            self.count += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def refresh(directory, *, paged=False, part_frames=256):
    index = directory / "index.html"
    original = index.read_bytes()
    parser = ConfigParser()
    parser.feed(original.decode("utf-8"))
    if parser.count != 1:
        raise ValueError("Expected one player config")
    config = json.loads("".join(parser.parts))
    frames = directory / "frames.bin"
    if config.get("schema") != 1 or frames.stat().st_size != config["matrix_bytes"]:
        raise ValueError("Player config/frames size mismatch")
    crc = 0
    with frames.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            crc = zlib.crc32(block, crc)
    if crc & 0xffffffff != config["matrix_crc32"]:
        raise ValueError("Frame checksum mismatch; original files preserved")
    if paged:
        config["parts"] = partition_matrices(frames, frames_per_part=part_frames, expected_crc=config["matrix_crc32"])
    js = (ASSETS / "player.js").read_text(encoding="utf-8")
    html = (ASSETS / "player.html").read_text(encoding="utf-8")
    html = html.replace("__CONFIG__", json.dumps(config, separators=(",", ":")).replace("</", "<\\/"))
    html = html.replace("__DATA__", "").replace('<script src="player.js"></script>',
                                               "<script>\n" + js.replace("</", "<\\/") + "\n</script>")
    with tempfile.NamedTemporaryFile(dir=directory, prefix="index-backup-", suffix=".html", delete=False) as backup:
        backup.write(original)
        backup_path = Path(backup.name)
    with tempfile.NamedTemporaryFile(dir=directory, prefix="index-update-", suffix=".tmp", delete=False) as target:
        target.write(html.encode("utf-8"))
        temporary = Path(target.name)
    try:
        temporary.replace(index)
    finally:
        temporary.unlink(missing_ok=True)
    return {"index": str(index), "backup": str(backup_path), "transfer_id": config["transfer_id"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--paged", action="store_true", help="Split existing matrices into loadable pages; preserve transfer ID")
    parser.add_argument("--part-frames", type=int, default=256)
    args = parser.parse_args()
    print(json.dumps(refresh(args.directory, paged=args.paged, part_frames=args.part_frames)))
