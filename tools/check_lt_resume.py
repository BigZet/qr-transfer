"""Replay a stopped LT journal on a copy; complete using a matching local archive.

Stop the receiver first. This checks persistence/FEC, not screen capture or VDI.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_transfer.fec import FecSession, pool
from qr_transfer.protocol import sha256_file
from qr_transfer.interfaces import State


def run(source, archive, output):
    archive_hash = sha256_file(archive)
    output.mkdir(parents=True, exist_ok=False)
    # SQLite backup gives a consistent snapshot and never rewrites source status.
    with sqlite3.connect((source / "fec.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(output / "fec.sqlite3") as target:
            original.backup(target)
    started = time.perf_counter()
    with FecSession(output, resume=True) as session:
        initial = session.snapshot()
        if not session.boot or session.descriptor.sha256 != archive_hash or session.descriptor.object_size != archive.stat().st_size:
            raise ValueError("Archive does not match the saved LT transfer")
        delivered = 0
        for raw in pool(archive, session.boot, session.transfer_id):
            session.feed(raw)
            delivered += 1
            if session.state == State.VERIFIED:
                break
        final = session.snapshot()
        exact = final["content_verified"] and sha256_file(output / "object.bin") == archive_hash
        report = {"scope": "local copied journal replay, no screen/VDI", "initial": initial,
                  "final": final, "packets_delivered": delivered,
                  "elapsed_seconds": time.perf_counter() - started, "exact_archive": exact}
        (output / "resume-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if not exact:
            raise RuntimeError("LT replay did not restore the archive")
        print(json.dumps({"initial_blocks": initial["restored_blocks"], "final_blocks": final["restored_blocks"],
                          "exact_archive": exact, "elapsed_seconds": report["elapsed_seconds"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.state, args.archive, args.output)
