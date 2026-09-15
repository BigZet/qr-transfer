"""Encrypted RGB browser round-trips and comparable mono/4/8-color runs. No VDI."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qr_transfer.capture import receive_frames
from qr_transfer.container import prepare, extract
from qr_transfer.pipeline import MultiDecoder
from qr_transfer.player import render, estimate
from qr_transfer.rgb import RGBDecoder
from qr_transfer.transfer import prepare_object
from qr_transfer.storage import DurableSession
from qr_transfer.fec import FecSession


def run(out):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=False)
    source = out / "synthetic.bin"
    source.write_bytes(hashlib.shake_256(b"i06-browser").digest(32768))
    archive = prepare(source, out / "object.7z", "synthetic-i06-only")
    transfer = prepare_object(archive, container="7z-aes256")
    report = {"schema": 1, "evidence_level": "headless_fullhd_no_vdi", "runs": [], "generation": {}, "weak_layer": [], "cache_comparison": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            for transport in ("repeat", "lt"):
                player = out / f"player-{transport}"
                report["generation"][transport] = render(archive, transfer.descriptor, player,
                                                         transport=transport, visual="rgb8", slots=1, interval_ms=300)
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                page.goto((player / "standalone.html").resolve().as_uri())
                page.wait_for_function("window.QRTransfer && QRTransfer.snapshot().ready")
                page.click("#fullscreen")
                for slots in (1, 2):
                    for visual in ("mono", "rg4", "rgb8"):
                        name = f"{transport}-{slots}-{visual}"
                        # Controls remain available in DOM while the stage is fullscreen.
                        page.select_option("#slots", str(slots), force=True)
                        page.select_option("#visual", visual, force=True)
                        page.evaluate("QRTransfer.reset()")
                        snapshot = page.evaluate("QRTransfer.snapshot()")
                        expected = estimate(transfer.descriptor, transport=transport, visual=visual, slots=slots, interval_ms=300)
                        assert snapshot["cycle_frames"] == expected["cycle_frames"], (snapshot, expected)
                        decoder = MultiDecoder() if visual == "mono" else RGBDecoder(visual)
                        page.evaluate("QRTransfer.start()")
                        result = receive_frames(out / name,
                            lambda: Image.open(io.BytesIO(page.screenshot())).convert("RGB"), decoder,
                            transport=transport, first_timeout=10, idle_timeout=20, total_timeout=30)
                        page.evaluate("QRTransfer.pause()")
                        assert result["exit_code"] == 0, (name, result)
                        restored = extract(out/name/"object.bin", out/f"restored-{name}",
                                           "synthetic-i06-only", descriptor=transfer.descriptor)
                        assert (restored/"synthetic.bin").read_bytes() == source.read_bytes()
                        row = {"name": name, "transport": transport, "slots": slots, "visual": visual,
                               "capture": result, "decoder": dict(decoder.stats),
                               "player": page.evaluate("QRTransfer.snapshot()"), "exact_restore": True}
                        report["runs"].append(row)
                        print(name, round(result["elapsed_seconds"], 3), "exact", flush=True)
                        (out/"report.json").write_text(json.dumps(report, indent=2)+"\n")
                # Same packetization, blue layer entirely absent at decoder output.
                # Deterministic stepping separates recovery from capture frame rate.
                page.select_option("#slots", "1", force=True)
                page.select_option("#visual", "rgb8", force=True)
                page.evaluate("QRTransfer.reset()")
                snapshot = page.evaluate("QRTransfer.snapshot()")
                session_type = FecSession if transport == "lt" else DurableSession
                decoder = RGBDecoder()
                with session_type(out/f"weak-{transport}") as session:
                    count = 0
                    for _ in range(snapshot["cycle_frames"] * 2):
                        picture = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                        for item in decoder(picture):
                            if item.layer != 2:
                                session.receiver.feed(item.raw)
                        count += 1
                        if session.snapshot()["content_verified"]:
                            break
                        page.evaluate("QRTransfer.step()")
                    state = session.snapshot()
                    assert state["content_verified"] == (transport == "lt"), state
                    report["weak_layer"].append({"transport": transport, "shown_tiles": count, "state": state,
                                                 "method": "drop all decoded blue-layer symbols; manual browser stepping"})
                if transport == "lt":
                    restored = extract(out/"weak-lt/object.bin", out/"restored-weak-lt", "synthetic-i06-only", descriptor=transfer.descriptor)
                    assert (restored/"synthetic.bin").read_bytes() == source.read_bytes()
                print(transport, "weak layer verified:", state["content_verified"], flush=True)

                # Stable content: compare bounded fingerprint cache and always decode.
                page.evaluate("QRTransfer.step();QRTransfer.step()")
                picture = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                for skip in (False, True):
                    decoder = RGBDecoder(skip_unchanged=skip, search_seconds=100)
                    decoder(picture)  # warm geometry
                    started = time.perf_counter()
                    for _ in range(20):
                        decoder(picture)
                    report["cache_comparison"].append({"transport": transport, "skip_unchanged": skip,
                        "frames": 20, "seconds": time.perf_counter()-started, "decoder": dict(decoder.stats)})

                # Operator calibration and mono/color switches preserve transfer ID.
                before_id = page.evaluate("QRTransfer.snapshot().transfer_id")
                page.locator("#calibrate").dispatch_event("click")
                assert page.evaluate("QRTransfer.snapshot().calibrating")
                service_decoder = RGBDecoder()
                assert not service_decoder(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
                assert service_decoder.stats["service_symbols"]
                page.select_option("#visual", "mono", force=True)
                page.select_option("#visual", "rgb8", force=True)
                assert before_id == page.evaluate("QRTransfer.snapshot().transfer_id")
                (out/"report.json").write_text(json.dumps(report, indent=2)+"\n")
                page.close()
        finally:
            browser.close()
    return report


def geometry_check(player, out):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            for dpr in (1, 1.25, 1.8):
                page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=dpr)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(player.resolve().as_uri())
                page.wait_for_function("window.QRTransfer && QRTransfer.snapshot().ready")
                page.click("#fullscreen")
                page.select_option("#visual", "rgb8", force=True)
                decoder = RGBDecoder()
                for width, height in ((1920,1080), (1280,720), (3840,2160), (1920,1080)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(100)
                    page.select_option("#slots", "2", force=True)
                    for mode in ("sync", "staggered"):
                        page.select_option("#mode", mode, force=True)
                        page.evaluate("QRTransfer.reset();QRTransfer.step();QRTransfer.step()")
                        shot = page.screenshot()
                        result = decoder(Image.open(io.BytesIO(shot)).convert("RGB"))
                        snapshot = page.evaluate("QRTransfer.snapshot()")
                        assert len(result) == snapshot["slots"]*3, (dpr, width, height, mode, snapshot)
                        assert not errors, errors
                        rows.append({"dpr":dpr, "viewport":[width,height], "mode":mode,
                                     "decoded_layers":len(result), "geometry":snapshot["geometry"],
                                     "calibration":decoder.calibration})
                        if dpr == 1 and width == 1920 and mode == "sync":
                            (out/"rgb8-fullhd.png").write_bytes(shot)
                page.close()
        finally:
            browser.close()
    report = {"evidence_level":"headless_geometry_no_vdi", "cases":rows}
    (out/"report.json").write_text(json.dumps(report, indent=2)+"\n")
    print("passed",len(rows),"geometry cases")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--geometry-player", type=Path, help="Only check geometry of this generated standalone HTML")
    args = parser.parse_args()
    if args.geometry_player:
        geometry_check(args.geometry_player, args.output)
    else:
        run(args.output)
