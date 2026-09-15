"""Headless Full HD visual round-trip, plus a small timed capture-loop test.

python tools/check_i03_player.py --output artifacts/i03/check
Requires .[sender,receiver,qr], playwright and Chrome. No real VDI results.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import io
import json
from pathlib import Path
import sys
import subprocess
import re
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qr_transfer.container import prepare, extract
from qr_transfer.player import render, matrix, pack_matrix
from qr_transfer.protocol import Packet, encode
from qr_transfer.transfer import prepare_object, Receiver
from qr_transfer.capture import receive_frames


def encoder_benchmark(selected=None):
    import psutil
    from PIL import Image
    import zxingcpp
    results = []
    for generator in ((selected,) if selected else ("qrcode", "segno")):
        mat_time = packed_time = png_time = 0
        rss = 0
        for index in range(8):
            data = b"".join(hashlib.sha256(f"{index}/{i}".encode()).digest() for i in range(87))[:2784]
            raw = encode(Packet(1, bytes(range(16)), index, 8, data))
            start = time.perf_counter(); rows = matrix(raw, generator); mat_time += time.perf_counter() - start
            start = time.perf_counter(); packed = pack_matrix(rows); packed_time += time.perf_counter() - start
            start = time.perf_counter()
            picture = Image.new("1", (185,185), 1)
            picture.putdata([1 if x < 4 or x >= 181 or y < 4 or y >= 181 else 1 - int(rows[y-4][x-4]) for y in range(185) for x in range(185)])
            output = io.BytesIO(); picture.resize((925,925), Image.Resampling.NEAREST).save(output, format="PNG")
            png_time += time.perf_counter() - start
            assert bytes(zxingcpp.read_barcode(Image.open(io.BytesIO(output.getvalue()))).bytes) == raw
            rss = max(rss, psutil.Process().memory_info().rss)
        results.append({"generator":generator, "samples":8, "matrix_seconds_mean":mat_time/8,
                        "pack_seconds_mean":packed_time/8, "png_seconds_mean":png_time/8,
                        "matrix_bytes":len(packed), "png_bytes_last":len(output.getvalue()),
                        "rss_observed_bytes":rss, "memory_note":"observed after decode, includes Pillow/ZXing; not a sampled peak"})
    return results


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def check(output, size_kib, runs):
    from PIL import Image
    import zxingcpp
    from playwright.sync_api import sync_playwright
    output.mkdir(parents=True, exist_ok=False)
    comparison = []
    for generator in ("qrcode", "segno"):
        data = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), "--encoder-benchmark", generator])
        comparison.extend(json.loads(data))
    report = {"schema":1, "evidence_level":"headless_fullhd_no_vdi", "encoder_comparison":comparison, "runs":[]}
    fixture = output / "source.bin"
    fixture.write_bytes(b"".join(hashlib.sha256(b"i03-v1" + i.to_bytes(8,"big")).digest() for i in range(size_kib * 32)))
    password = "synthetic-i03-only"
    started = time.perf_counter()
    archive = prepare(fixture, output / "object.7z", password)
    report["pack_seconds"] = time.perf_counter() - started
    transfer = prepare_object(archive, container="7z-aes256")
    import psutil
    stop_sample = threading.Event(); memory = [0]
    def sample():
        while not stop_sample.is_set():
            memory[0] = max(memory[0], psutil.Process().memory_info().rss)
            stop_sample.wait(.01)
    sampler = threading.Thread(target=sample, daemon=True); sampler.start()
    try:
        report["generation"] = render(archive, transfer.descriptor, output / "player", progress=lambda n,t: print(f"Generated {n}/{t}",flush=True))
    finally:
        stop_sample.set(); sampler.join()
    report["generation"]["python_peak_rss_sampled"] = memory[0]
    handler = functools.partial(QuietHandler, directory=str((output / "player").resolve()))
    server = http.server.ThreadingHTTPServer(("127.0.0.1",0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            errors = []
            page = browser.new_page(viewport={"width":1920,"height":1080}, device_scale_factor=1)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url); page.wait_for_function("window.QRTransfer && window.QRTransfer.snapshot().ready")
            page.click("#fullscreen"); page.wait_for_timeout(100)
            report["geometry"] = page.evaluate("QRTransfer.snapshot()")
            session = page.context.new_cdp_session(page); session.send("Performance.enable")
            report["browser_metrics"] = {x["name"]:x["value"] for x in session.send("Performance.getMetrics")["metrics"] if x["name"] in ("JSHeapUsedSize", "JSHeapTotalSize")}
            assert report["geometry"]["geometry"]["canvas_backing"] == [925,925]
            assert report["geometry"]["geometry"]["canvas_origin_css"][1] >= 48
            page.screenshot(path=str(output / "fullhd.png"))
            # Loss of initial metadata and a middle frame; no sender feedback.
            for run in range(runs):
                receiver = Receiver(output / f"receive-{run}")
                page.evaluate("QRTransfer.reset(); for(let i=0;i<5;i++) QRTransfer.step()")
                started = time.perf_counter(); attempts = 0
                max_frames = report["generation"]["cycle_frames"] * 2
                for i in range(max_frames):
                    if i != 7:
                        shot = page.locator("#qr").screenshot(animations="disabled")
                        code = zxingcpp.read_barcode(Image.open(io.BytesIO(shot)))
                        assert code is not None, f"Missing QR at frame {i}"
                        receiver.feed(bytes(code.bytes)); attempts += 1
                    if receiver.snapshot()["content_verified"]:
                        break
                    page.evaluate("QRTransfer.step()")
                assert receiver.snapshot()["content_verified"]
                receive_seconds = time.perf_counter() - started
                started = time.perf_counter()
                restored = extract(receiver.directory / "object.bin", output / f"restored-{run}", password, descriptor=receiver.descriptor)
                assert (restored / fixture.name).read_bytes() == fixture.read_bytes()
                report["runs"].append({"run":run+1, "ok":True, "frames_decoded":attempts,
                                       "visual_step_seconds":receive_seconds, "extract_seconds":time.perf_counter()-started,
                                       "source_bytes":fixture.stat().st_size, "object_bytes":archive.stat().st_size,
                                       "sha256_source":hashlib.sha256(fixture.read_bytes()).hexdigest(),
                                       "note":"manual steps, not 600 ms throughput"})
                print(f"Full HD roundtrip {run+1}/{runs}: OK",flush=True)
            page.evaluate("QRTransfer.reset(); QRTransfer.start(); QRTransfer.start()")
            page.wait_for_timeout(1350); page.evaluate("QRTransfer.pause()")
            before = page.evaluate("QRTransfer.snapshot()")
            assert 1 <= before["updates"] <= 3
            shot = page.locator("#qr").screenshot()
            page.wait_for_timeout(200)
            assert page.locator("#qr").screenshot() == shot
            page.evaluate("QRTransfer.step()")
            assert page.evaluate("QRTransfer.snapshot().cursor") != before["cursor"]
            page.evaluate("document.exitFullscreen()")
            page.locator("#interval").fill("100"); page.locator("#interval").dispatch_event("change")
            page.evaluate("QRTransfer.start()")
            page.evaluate("const until=performance.now()+500; while(performance.now()<until){}")
            stalled = page.evaluate("QRTransfer.snapshot().updates")
            page.wait_for_timeout(250); page.evaluate("QRTransfer.pause()")
            assert page.evaluate("QRTransfer.snapshot().updates") - stalled <= 3
            page.reload(); page.wait_for_function("QRTransfer.snapshot().ready")
            assert page.evaluate("QRTransfer.snapshot().updates") == 0
            page.evaluate("QRTransfer.start(); Object.defineProperty(document,'hidden',{configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'))")
            assert page.evaluate("QRTransfer.snapshot().running") is False
            page.reload(); page.wait_for_function("QRTransfer.snapshot().ready")
            # Non-integer DPR still uses integral backing-pixel modules.
            for dpr in (1.25, 1.8):
                other = browser.new_page(viewport={"width":1920,"height":1080}, device_scale_factor=dpr)
                other.goto(url); other.wait_for_function("QRTransfer.snapshot().ready")
                other.click("#fullscreen"); other.wait_for_timeout(100)
                geometry = other.evaluate("QRTransfer.snapshot().geometry")
                assert geometry["canvas_backing"][0] == 185 * geometry["module_backing_px"]
                code = zxingcpp.read_barcode(Image.open(io.BytesIO(other.locator("#qr").screenshot())))
                assert code is not None
                other.close()
            # Standalone must work with every network request blocked.
            standalone = browser.new_page(viewport={"width":1920,"height":1080})
            standalone.route(re.compile(r"^https?://"), lambda route: route.abort())
            standalone.goto((output / "player/standalone.html").resolve().as_uri())
            standalone.wait_for_function("QRTransfer.snapshot().ready")
            report["standalone"] = standalone.evaluate("QRTransfer.snapshot()")
            assert standalone.evaluate("document.querySelectorAll('script[src]').length") == 0
            # A real-time rAF player, sampled through the production receive loop.
            # The grab adapter here is a browser PNG, not mss / not VDI.
            tiny = output / "tiny-tree"; (tiny / "empty").mkdir(parents=True)
            (tiny / "data.bin").write_bytes(fixture.read_bytes()[:16384])
            (tiny / "text.txt").write_text("Unicode: \u041f\u0440\u0438\u0432\u0435\u0442", encoding="utf-8")
            tiny_archive = prepare(tiny, output / "tiny.7z", password)
            tiny_transfer = prepare_object(tiny_archive, container="7z-aes256")
            render(tiny_archive, tiny_transfer.descriptor, output / "player/tiny")
            page.goto(url.replace("index.html", "tiny/index.html")); page.wait_for_function("QRTransfer.snapshot().ready")
            page.click("#fullscreen"); page.wait_for_timeout(100); page.evaluate("QRTransfer.start()")
            def decoder(raw):
                code = zxingcpp.read_barcode(Image.open(io.BytesIO(raw)))
                return bytes(code.bytes) if code is not None else None
            timed = receive_frames(output / "timed-state", lambda: page.screenshot(), decoder,
                                   first_timeout=15, idle_timeout=15, total_timeout=30)
            page.evaluate("QRTransfer.pause()")
            assert timed["exit_code"] == 0
            restored = extract(output / "timed-state/object.bin", output / "timed-restored", password,
                               descriptor=tiny_transfer.descriptor)
            assert (restored / "tiny-tree/data.bin").read_bytes() == (tiny / "data.bin").read_bytes()
            timed["evidence_level"] = "timed_headless_png_capture_no_vdi"
            report["timed_small_tree"] = timed
            page.keyboard.press("Space")
            assert page.evaluate("QRTransfer.snapshot().running")
            page.keyboard.press("Space")
            assert not page.evaluate("QRTransfer.snapshot().running")
            tiny_frames = (output / "player/tiny/frames.bin").read_bytes()
            invalid_frames = [tiny_frames[:-1], tiny_frames + b"x", tiny_frames[:-1] + bytes([tiny_frames[-1] ^ 1])]
            for invalid in invalid_frames:
                broken = browser.new_page(viewport={"width":1920,"height":1080})
                broken.route("**/frames.bin", lambda route: route.fulfill(body=invalid, content_type="application/octet-stream"))
                broken.goto(url.replace("index.html", "tiny/index.html"))
                broken.wait_for_function("window.QRTransfer && QRTransfer.snapshot().events.some(x=>x.type==='load_failed')")
                assert not broken.evaluate("QRTransfer.snapshot().ready")
                assert broken.locator("#start").is_disabled()
                broken.close()
            html = (output / "player/standalone.html").read_text(encoding="utf-8")
            assert password not in html and fixture.name not in html
            assert not errors, errors
            browser.close()
        report["ok"] = True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--encoder-benchmark", choices=("segno","qrcode"))
    parser.add_argument("--size-kib", type=int, default=1024)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.encoder_benchmark:
        print(json.dumps(encoder_benchmark(args.encoder_benchmark)))
    else:
        if args.output is None or not 1 <= args.size_kib <= 4096 or not 1 <= args.runs <= 3:
            parser.error("Specify output, 1..4096 KiB and 1..3 runs")
        check(args.output, args.size_kib, args.runs)
