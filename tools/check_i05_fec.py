"""Equal-budget repeat/LT comparison and encrypted browser round-trip. No VDI."""

from __future__ import annotations
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qr_transfer.container import prepare, extract
from qr_transfer.transfer import prepare_object, packets
from qr_transfer.storage import DurableSession
from qr_transfer.fec import bootstrap, pool, FecSession, decode
from qr_transfer.player import render
from qr_transfer.capture import receive_frames
from qr_transfer.pipeline import MultiDecoder


def scheduled(sequence):
    meta = sequence[0]
    result = [meta]
    for i, raw in enumerate(sequence[1:], 1):
        result.append(raw)
        if i % 8 == 0:
            result.append(meta)
    return result


def run(out, seeds=20):
    import psutil

    out.mkdir(parents=True, exist_ok=False)
    source = out / "source.bin"
    source.write_bytes(hashlib.shake_256(b"i05-benchmark").digest(131072))
    archive = prepare(source, out / "object.7z", "synthetic-i05-only", profile="strong")
    transfer = prepare_object(archive, container="7z-aes256")
    start = time.perf_counter()
    sequences = {"repeat": list(packets(transfer))}
    generation = {}
    for mode in ("systematic", "repair-only"):
        start = time.perf_counter()
        boot = bootstrap(transfer.descriptor, mode=mode)
        sequences[mode] = scheduled(list(pool(archive, boot, transfer.transfer_id)))
        generation[mode] = {
            "seconds": time.perf_counter() - start,
            "symbols": boot.symbols,
            "frames": len(sequences[mode]),
        }
    budget = len(sequences["repeat"]) * 8
    cases = [
        (f"loss-{int(rate * 100)}", rate, 0, 0, 0, 0)
        for rate in (0, 0.05, 0.1, 0.2, 0.4)
    ]
    cases += [
        (f"burst-{seconds}s", 0.1, math.ceil(seconds / 0.3), 0, 0, 0)
        for seconds in (0.5, 2, 5)
    ]
    cases += [("late-duplicates-corrupt-reorder", 0.1, 0, 12, 0.1, 0.02)]
    report = {
        "schema": 1,
        "evidence_level": "synthetic_packets_no_vdi",
        "seeds": seeds,
        "symbol_interval_seconds": 0.3,
        "shown_budget": budget,
        "object_bytes": archive.stat().st_size,
        "generation": generation,
        "runs": [],
        "traces": [],
    }
    process = psutil.Process()
    report["rss_before"] = process.memory_info().rss
    peak = report["rss_before"]
    started = time.perf_counter()
    for name, loss, burst, late, dup, corrupt in cases:
        for seed in range(seeds):
            rng = random.Random(seed)
            order = list(range(budget))
            if late:
                for i in range(0, budget - 4, 4):
                    part = order[i : i + 4]
                    rng.shuffle(part)
                    order[i : i + 4] = part
            drops = {i for i in range(budget) if i < late or rng.random() < loss}
            if burst:
                first = 8 + seed % 12
                drops.update(range(first, first + burst))
            doubles = {i for i in range(budget) if rng.random() < dup}
            flips = {i for i in range(budget) if rng.random() < corrupt}
            trace = {
                "case": name,
                "seed": seed,
                "order": order,
                "drop": sorted(drops),
                "duplicate": sorted(doubles),
                "corrupt": sorted(flips),
            }
            report["traces"].append(trace)
            for mode, seq in sequences.items():
                with tempfile.TemporaryDirectory(dir=out) as root:
                    constructor = DurableSession if mode == "repeat" else FecSession
                    t0 = time.perf_counter()
                    finished = None
                    received = 0
                    with constructor(Path(root) / "state") as state:
                        for position, slot in enumerate(order):
                            if slot in drops:
                                continue
                            raw = seq[slot % len(seq)]
                            if slot in flips:
                                raw = raw[:-1] + bytes([raw[-1] ^ 1])
                            state.receiver.feed(raw)
                            received += 1
                            if slot in doubles:
                                state.receiver.feed(raw)
                            if state.receiver.state.value == "object_verified":
                                finished = position + 1
                                break
                        snapshot = state.snapshot()
                    elapsed = time.perf_counter() - t0
                    peak = max(peak, process.memory_info().rss)
                    report["runs"].append(
                        {
                            "case": name,
                            "seed": seed,
                            "mode": mode,
                            "success": finished is not None,
                            "shown_to_completion": finished,
                            "modeled_seconds": finished * 0.3 if finished else None,
                            "extra_over_ideal_payload_seconds": max(
                                0, finished - transfer.descriptor.total
                            )
                            * 0.3
                            if finished
                            else None,
                            "received_packets": received,
                            "cpu_and_disk_seconds": elapsed,
                            "state_bytes": snapshot["state_bytes"],
                            "counters": snapshot["counters"],
                        }
                    )
        print(name, "done", flush=True)
    report["elapsed_seconds"] = time.perf_counter() - started
    report["rss_peak_observed"] = peak
    report["summary"] = []
    for name, *_ in cases:
        for mode in sequences:
            rows = [
                r for r in report["runs"] if r["case"] == name and r["mode"] == mode
            ]
            values = sorted(r["modeled_seconds"] for r in rows if r["success"])
            report["summary"].append(
                {
                    "case": name,
                    "mode": mode,
                    "successes": len(values),
                    "attempts": len(rows),
                    "median_seconds_successful": statistics.median(values)
                    if values
                    else None,
                    "p90_seconds_successful": values[math.ceil(len(values) * 0.9) - 1]
                    if values
                    else None,
                }
            )
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    # Persist interoperability vector: deterministic object, fixed transfer ID and first repair packet.
    boot = bootstrap(transfer.descriptor)
    seq = list(pool(archive, boot, b"\x15" * 16))
    vector = {
        "backend": "lt-code==0.3",
        "bootstrap_hex": seq[0].hex(),
        "source_hex": seq[1].hex(),
        "first_repair_hex": next(r for r in seq if decode(r).kind == 2).hex(),
        "archive_hex": archive.read_bytes().hex(),
    }
    (out / "vector.json").write_text(json.dumps(vector, indent=2) + "\n")
    return report


def browser_check(out):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=False)
    source = out / "browser-source.bin"
    source.write_bytes(hashlib.shake_256(b"i05-browser").digest(64000))
    archive = prepare(source, out / "object.7z", "synthetic-i05-only")
    transfer = prepare_object(archive, container="7z-aes256")
    t0 = time.perf_counter()
    generation = render(
        archive,
        transfer.descriptor,
        out / "player",
        transport="lt",
        slots=2,
        interval_ms=250,
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto((out / "player/standalone.html").resolve().as_uri())
            page.wait_for_function("window.QRTransfer && QRTransfer.snapshot().ready")
            page.click("#fullscreen")
            page.wait_for_timeout(100)
            decoder = MultiDecoder()
            number = [0]

            def read(image):
                number[0] += 1
                result = decoder(image)
                # Erase one slot periodically, plus late join before the next bootstrap.
                return result[:1] if number[0] % 3 == 0 else result

            page.evaluate("QRTransfer.step();QRTransfer.step();QRTransfer.start()")
            result = receive_frames(
                out / "state",
                lambda: Image.open(io.BytesIO(page.screenshot())).convert("RGB"),
                read,
                transport="lt",
                first_timeout=10,
                idle_timeout=30,
                total_timeout=60,
            )
            page.evaluate("QRTransfer.pause()")
            assert result["exit_code"] == 0, result
            restored = extract(
                out / "state/object.bin",
                out / "restored",
                "synthetic-i05-only",
                descriptor=transfer.descriptor,
            )
            assert (restored / "browser-source.bin").read_bytes() == source.read_bytes()
            report = {
                "schema": 1,
                "evidence_level": "headless_fullhd_no_vdi",
                "generation": generation,
                "capture": result,
                "player": page.evaluate("QRTransfer.snapshot()"),
                "exact_restore": True,
                "elapsed_including_generation": time.perf_counter() - t0,
            }
            with FecSession(out / "state", resume=True) as state:
                report["resume_seconds"] = state.resume_seconds
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        finally:
            browser.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--browser-only", action="store_true")
    args = parser.parse_args()
    if args.browser_only:
        browser_check(args.output)
    else:
        run(args.output, args.seeds)
