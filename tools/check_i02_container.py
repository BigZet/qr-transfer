"""Reproducible local compression comparison. Uses only synthetic test data.

python tools/check_i02_container.py --output artifacts/i02/benchmark
Requires .[container] and psutil (installed with py7zr). No VDI measurements.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qr_transfer.container import PROFILES, prepare, extract
from qr_transfer.transfer import prepare_object


def worker(args):
    # Synthetic benchmark only; never obtain a real user password here.
    password = "synthetic-benchmark-only"
    started = time.perf_counter()
    archive = prepare(args.source, args.output, password, profile=args.profile)
    seconds = time.perf_counter() - started
    descriptor = prepare_object(archive, container="7z-aes256").descriptor
    extract(archive, args.output.with_suffix(".restored"), password, descriptor=descriptor)
    print(json.dumps({"profile": args.profile, "object_bytes": archive.stat().st_size,
                      "prepare_seconds": seconds, "manifest_verified": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--profile", choices=PROFILES)
    args = parser.parse_args()
    if args.source:
        worker(args)
        return
    import psutil
    args.output.mkdir(parents=True, exist_ok=False)
    fixtures = args.output / "fixtures"
    fixtures.mkdir()
    text = b"".join(f"{i:08d} repeated diagnostic record: alpha beta gamma delta\n".encode() for i in range(40000))
    random_bytes = b"".join(hashlib.sha256(b"i02-v1" + i.to_bytes(4, "big")).digest() for i in range(65536))
    (fixtures / "text.txt").write_bytes(text)
    # Larger than the default dictionary: exposes the compression memory cost.
    with (fixtures / "large-text.txt").open("wb") as target:
        remaining = 48 << 20
        while remaining:
            block = text[:remaining]
            target.write(block)
            remaining -= len(block)
    (fixtures / "compressed.zlib").write_bytes(zlib.compress(random_bytes, 9))
    tree = fixtures / "tree"
    (tree / "empty").mkdir(parents=True)
    for i in range(16):
        (tree / f"part{i:02d}.txt").write_bytes(text[i * 100000:(i + 1) * 100000])
    (tree / "random.bin").write_bytes(random_bytes[:256 << 10])
    (tree / "empty.bin").touch()
    report = {"schema": 1, "evidence_level": "local_disk_no_vdi", "python": platform.python_version(),
              "os": platform.system(), "profiles": {name: {"preset": value[0], "dictionary_bytes": value[1]} for name, value in PROFILES.items()},
              "package_versions": {name: importlib.metadata.version(name) for name in
                                   ("py7zr", "pycryptodomex", "psutil", "backports.zstd", "brotli", "inflate64", "multivolumefile", "pybcj", "pyppmd", "texttable")},
              "results": [], "memory_note": "10 ms sampled sum RSS, entire child process tree, includes extract; shared pages may be counted twice"}
    for case in ("text.txt", "compressed.zlib", "tree", "large-text.txt"):
        source = fixtures / case
        files = [source] if source.is_file() else [p for p in source.rglob("*") if p.is_file()]
        input_size = sum(p.stat().st_size for p in files)
        for profile in PROFILES:
            command = [sys.executable, str(Path(__file__).resolve()), "--source", str(source.resolve()),
                       "--output", str((args.output / f"{case}-{profile}.7z").resolve()), "--profile", profile]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            peak = 0
            deadline = time.monotonic() + 300
            while process.poll() is None:
                if time.monotonic() > deadline:
                    process.kill(); process.communicate()
                    raise RuntimeError("Benchmark timed out")
                try:
                    parent = psutil.Process(process.pid)
                    peak = max(peak, sum(p.memory_info().rss for p in [parent] + parent.children(recursive=True)))
                except psutil.Error:
                    pass
                time.sleep(.01)
            stdout, stderr = process.communicate()
            if process.returncode:
                raise RuntimeError(f"Benchmark failed: {case}/{profile}: {stderr.decode(errors='replace')}")
            row = json.loads(stdout)
            row.update(case=case, source_bytes=input_size, peak_rss_bytes=peak)
            report["results"].append(row)
            print(f"{case}/{profile}: {row['object_bytes']} bytes, {row['prepare_seconds']:.2f}s, RSS {peak / (1 << 20):.1f} MiB", flush=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
