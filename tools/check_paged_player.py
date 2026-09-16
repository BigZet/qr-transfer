"""Local HTTP page boundaries, encrypted transfer and browser buffer validation."""
import argparse
from functools import partial
import hashlib
import http.server
import io
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qr_transfer.container import prepare, extract
from qr_transfer.transfer import prepare_object
from qr_transfer.player import render
from qr_transfer.capture import receive_frames
from qr_transfer.rgb import RGBDecoder


def run(out):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True, exist_ok=False)
    source = out / "synthetic.bin"
    source.write_bytes(hashlib.shake_256(b"paged-v1").digest(128 << 10))
    archive = prepare(source, out / "object.7z", "synthetic-paged-only")
    transfer = prepare_object(archive, container="7z-aes256")
    report = {"evidence_level":"local_http_headless_no_vdi", "runs":[]}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(out.resolve())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                for transport in ("repeat", "lt"):
                    generation = render(archive, transfer.descriptor, out/transport, transport=transport,
                                        visual="rgb8", slots=2, interval_ms=150, part_frames=8)
                    page = browser.new_page(viewport={"width":1920,"height":1080})
                    errors, requests = [], []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.on("request", lambda r: requests.append(r.url))
                    page.goto(f"http://127.0.0.1:{server.server_port}/{transport}/index.html")
                    page.wait_for_function("window.QRTransfer && QRTransfer.snapshot().ready")
                    initial = page.evaluate("QRTransfer.snapshot()")
                    assert initial["paging"]["enabled"]
                    assert initial["paging"]["requests"] < generation["parts"]
                    page.click("#fullscreen")
                    decoder = RGBDecoder()
                    page.evaluate("QRTransfer.start()")
                    result = receive_frames(out/f"state-{transport}",
                        lambda: Image.open(io.BytesIO(page.screenshot())).convert("RGB"), decoder,
                        transport=transport, first_timeout=15, idle_timeout=30, total_timeout=90)
                    page.evaluate("QRTransfer.pause()")
                    assert result["exit_code"] == 0, result
                    restored = extract(out/f"state-{transport}"/"object.bin", out/f"restored-{transport}",
                                       "synthetic-paged-only", descriptor=transfer.descriptor)
                    assert (restored/"synthetic.bin").read_bytes() == source.read_bytes()
                    # Visit every page, wrap, then change layout. No archive restart.
                    page.evaluate("QRTransfer.reset()")
                    for _ in range(page.evaluate("QRTransfer.snapshot().cycle_frames")+4):
                        page.wait_for_function("!QRTransfer.snapshot().paging.loading")
                        page.evaluate("QRTransfer.step()")
                        page.wait_for_function("!QRTransfer.snapshot().paging.loading")
                    for visual in ("mono","rg4","rgb8"):
                        page.select_option("#visual", visual, force=True)
                        page.wait_for_function("!QRTransfer.snapshot().paging.loading")
                    snapshot = page.evaluate("QRTransfer.snapshot()")
                    assert snapshot["paging"]["cached_parts"] <= 4
                    assert snapshot["paging"]["peak_buffer_bytes"] <= generation["matrix_buffer_limit_bytes"], snapshot
                    assert not errors, errors
                    assert not any(url.endswith("/frames.bin") or url.endswith("/player.js") for url in requests)
                    assert "цикл" in page.locator("#eta").inner_text()
                    report["runs"].append({"transport":transport, "generation":generation, "capture":result,
                                           "initial":initial, "final":snapshot, "exact_restore":True,
                                           "page_requests":len(requests)})
                    (out/"report.json").write_text(json.dumps(report, indent=2)+"\n")
                    print(transport, "exact, peak buffer", snapshot["paging"]["peak_buffer_bytes"], flush=True)
                    page.close()
            finally:
                browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
