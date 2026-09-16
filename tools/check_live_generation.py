"""Prove browser playback starts during QR generation and restores exact bytes."""
import argparse
from functools import partial
import hashlib
import http.server
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qr_transfer.container import prepare, extract
from qr_transfer.transfer import prepare_object
from qr_transfer.capture import receive_frames
from qr_transfer.rgb import RGBDecoder


def run(out, *, transport="repeat", workers=1, source_kib=256):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = out/"synthetic.bin"
    source.write_bytes(hashlib.shake_256(b"live-generation").digest(source_kib << 10))
    archive = prepare(source, out/"object.7z", "synthetic-live-only")
    transfer = prepare_object(archive, container="7z-aes256")
    (out/"descriptor.json").write_text(json.dumps(transfer.descriptor.__dict__))
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1",0), partial(Handler,directory=str(out)))
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    with (out/"render.log").open("w") as log:
        process = subprocess.Popen([sys.executable,"-m","qr_transfer","render",str(archive),
            "--descriptor",str(out/"descriptor.json"),"--output",str(out/"player"),"--workers",str(workers),"--transport",transport,
            "--live","--external-only","--part-frames","8","--visual","rgb8","--slots","2","--interval-ms","100"],
            cwd=ROOT,stdout=log,stderr=log)
        try:
            started = time.perf_counter()
            while not (out/"player/index.html").exists():
                assert process.poll() is None, "Producer failed before HTML publication"
                assert time.perf_counter()-started < 30
                time.sleep(.05)
            (out/"player/first-index.html").write_bytes((out/"player/index.html").read_bytes())
            first_html = time.perf_counter()-started
            assert process.poll() is None, "Playback must start before generation completes"
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome",headless=True)
                try:
                    page = browser.new_page(viewport={"width":1920,"height":1080})
                    errors = []
                    page.on("pageerror",lambda e:errors.append(str(e)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/player/first-index.html")
                    page.wait_for_function("window.QRTransfer && QRTransfer.snapshot().ready")
                    page.click("#fullscreen")
                    page.evaluate("QRTransfer.start()")
                    observed_wait = [False]
                    def grab():
                        observed_wait[0] |= page.evaluate("QRTransfer.snapshot().paging.generation_waiting")
                        return Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                    result = receive_frames(out/"state",grab,RGBDecoder(),transport=transport,first_timeout=15,idle_timeout=30,total_timeout=90)
                    page.evaluate("QRTransfer.pause()")
                    assert result["exit_code"] == 0, result
                    assert not errors, errors
                    assert observed_wait[0], "Producer wait must be exercised"
                    assert process.wait(timeout=120) == 0
                    restored = extract(out/"state/object.bin",out/"restored","synthetic-live-only",descriptor=transfer.descriptor)
                    assert (restored/"synthetic.bin").read_bytes() == source.read_bytes()
                    report = {"scope":"local HTTP, slow producer, no VDI", "first_html_seconds":first_html,
                              "opened_before_completion":True,"generation_wait_observed":True,"exact_restore":True,
                              "prepare":json.loads((out/"player/prepare.json").read_text()),"capture":result,
                              "browser":page.evaluate("QRTransfer.snapshot()")}
                    (out/"report.json").write_text(json.dumps(report,indent=2)+"\n")
                    print("live exact restore; first HTML",round(first_html,3),"seconds",flush=True)
                finally:
                    browser.close()
        finally:
            if process.poll() is None:
                process.terminate();process.wait(timeout=10)
            server.shutdown();server.server_close();thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--transport", choices=("repeat","lt"), default="repeat")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--source-kib", type=int, default=256)
    args = parser.parse_args()
    run(args.output, transport=args.transport, workers=args.workers, source_kib=args.source_kib)
