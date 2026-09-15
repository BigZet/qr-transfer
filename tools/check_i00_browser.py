"""Headless local browser checks. Serves only synthetic diagnostics on loopback.

Requires requirements/i00-dev.txt and an installed Chrome/Edge. Does not measure VDI.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import io
import json
import threading
import time
from pathlib import Path

from i00 import ROOT, digest, html_frames, load_legacy, provenance, verify_received, write_json


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check(args) -> int:
    from playwright.sync_api import sync_playwright
    from PIL import Image
    import zxingcpp

    args.output.mkdir(parents=True, exist_ok=False)
    report = provenance("headless_local_browser_no_vdi")
    report["checks"] = []
    serve = functools.partial(Handler, directory=str(ROOT / "diagnostics/browser"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.channel, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                port = server.server_address[1]
                page.goto(f"http://127.0.0.1:{port}/index.html?token=do-not-export")
                page.wait_for_function("window.I00 && window.I00.snapshot().features.worker !== 'pending'")
                snapshot = page.evaluate("window.I00.snapshot()")
                for name in ("inline_script", "external_script", "relative_json_fetch", "worker", "wasm", "canvas"):
                    if snapshot["features"][name] != "ok":
                        raise AssertionError(f"Browser capability {name}: {snapshot['features'][name]}")
                if "do-not-export" in json.dumps(snapshot):
                    raise AssertionError("Page URL token leaked into report")
                report["checks"].append("offline assets, inline/external JS, JSON, worker, WASM; URL redaction")
                page.click("#start")
                page.click("#start")
                page.wait_for_timeout(1250)
                page.click("#pause")
                count = page.evaluate("window.I00.snapshot().playback.frame")
                if not 4 <= count <= 8:
                    raise AssertionError(f"Unexpected 5Hz frame count / duplicate timers: {count}")
                page.wait_for_timeout(300)
                if page.evaluate("window.I00.snapshot().playback.frame") != count:
                    raise AssertionError("Pause did not stop playback")
                page.click("#step")
                if page.evaluate("window.I00.snapshot().playback.frame") != count + 1:
                    raise AssertionError("Step did not advance exactly once")
                report["checks"].append("start idempotent; pause stable; step advances once")
                page.select_option("#cell", "3")
                page.set_viewport_size({"width": 1280, "height": 720})
                page.wait_for_timeout(100)
                resized = page.evaluate("window.I00.snapshot()")
                if resized["geometry"]["css_viewport"] != [1280, 720]:
                    raise AssertionError("Resize not reflected")
                if resized["geometry"]["canvas_backing"][0] >= snapshot["geometry"]["canvas_backing"][0]:
                    raise AssertionError("Canvas did not shrink")
                page.click("#fullscreen")
                page.wait_for_timeout(200)
                report["fullscreen_result"] = page.evaluate("window.I00.snapshot().features.fullscreen")
                if page.evaluate("!!document.fullscreenElement"):
                    page.evaluate("document.exitFullscreen()")
                page.click("#reset")
                if page.evaluate("window.I00.snapshot().playback.frame") != 0:
                    raise AssertionError("Reset failed")
                page.screenshot(path=str(args.output / "diagnostic.png"))
                report["browser"] = page.evaluate("window.I00.snapshot()")
                report["browser_version"] = browser.version
                report["checks"].append("resize, reset; fullscreen outcome recorded")
                page.reload()
                page.wait_for_function("window.I00 && window.I00.snapshot().features.worker !== 'pending'")
                if page.evaluate("window.I00.snapshot().playback.frame") != 0:
                    raise AssertionError("Reload state is stale")

                # Decode the actual browser-rendered legacy QR at the old 90vmin Full HD geometry.
                # This is independent of both PNG decoding and physical desktop/VDI capture.
                if args.prepared and args.fixtures:
                    receiver = load_legacy("i00_browser_receiver", "qr.py")
                    page.set_viewport_size({"width": 1920, "height": 1080})
                    report["legacy_browser_runs"] = []
                    for case in ("binary", "tree"):
                        for run in range(1, 4):
                            page.goto((args.prepared.resolve() / f"{case}.html").as_uri())
                            page.evaluate("running = false; restartTimer(); index = 0; render();")
                            frame_count = len(html_frames(args.prepared / f"{case}.html"))
                            chunks, metadata = {}, None
                            start = time.perf_counter()
                            for frame in range(frame_count):
                                page.evaluate("(i) => { index = i; render(); }", frame)
                                page.evaluate("document.getElementById('image').decode()")
                                png = page.screenshot()
                                barcode = zxingcpp.read_barcode(Image.open(io.BytesIO(png)).convert("RGB"))
                                if barcode is None:
                                    raise AssertionError(f"Browser QR failed: {case} frame {frame}")
                                packet = receiver.parse_packet(bytes(barcode.bytes))
                                if packet is None:
                                    raise AssertionError("Browser packet failed CRC")
                                if packet["type"] == receiver.TYPE_META:
                                    metadata = json.loads(packet["payload"])
                                else:
                                    chunks[packet["index"]] = packet["payload"]
                            data = b"".join(chunks[i] for i in range(1, packet["total"] + 1))
                            if metadata is None or digest(data) != metadata["sha256"] or len(data) != metadata["size"]:
                                raise AssertionError("Browser archive size/hash mismatch")
                            output = args.output / f"{case}-{run}.received"
                            output.write_bytes(data)
                            verified = verify_received(output, args.fixtures, case)
                            report["legacy_browser_runs"].append({"case": case, "run": run, "ok": True,
                                "frames": frame_count, "decode_verify_seconds": time.perf_counter() - start,
                                "viewport": [1920, 1080], "qr_css_size": page.locator("#image").bounding_box(),
                                "received": verified, "playback": "manually stepped; not 600ms throughput"})
                            print(f"Browser {case} {run}/3: OK", flush=True)
                if errors:
                    raise AssertionError(f"Browser JS errors: {errors}")
                report["ok"] = True
            finally:
                browser.close()
    except Exception as exc:
        report.update({"ok": False, "error_type": type(exc).__name__, "detail": str(exc)[:1000]})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        write_json(args.output / "browser-check.json", report)
    print(f"Browser checks: {'OK' if report['ok'] else 'FAILED'}; {args.output}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("chrome", "msedge"), default="chrome")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--fixtures", type=Path)
    args = parser.parse_args()
    if bool(args.prepared) != bool(args.fixtures):
        parser.error("--prepared and --fixtures are required together")
    raise SystemExit(check(args))
