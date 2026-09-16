"""Reproduce Jupyter HTML isolation without access to a VDI or its credentials."""
import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refresh_player import ConfigParser
from serve_player import PlayerHandler
from qr_transfer.player import _write_html


def run(source, output):
    from playwright.sync_api import sync_playwright
    shutil.copytree(source, output)
    parser = ConfigParser()
    parser.feed((output / "index.html").read_text(encoding="utf-8"))
    config = json.loads("".join(parser.parts))
    if config["matrix_bytes"] > 8 * 1024 * 1024:
        raise ValueError("Use a small completed fixture for this check")
    _write_html(output, config, standalone=True)

    class Handler(PlayerHandler):
        def end_headers(self):
            if self.path.endswith("?sandbox"):
                self.send_header("Content-Security-Policy", "sandbox allow-scripts")
            super().end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(output.resolve())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                for name, url in [("isolated", "index.html?sandbox"), ("served", "index.html"), ("standalone", "standalone.html?sandbox")]:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{server.server_port}/{url}")
                    if name == "isolated":
                        page.wait_for_function("document.getElementById('status').textContent.includes('origin=null')")
                        assert page.locator("#start").is_disabled()
                    else:
                        page.wait_for_function("!document.getElementById('start').disabled")
                    report[name] = {"opaque_origin": page.evaluate("window.origin === 'null'"),
                                    "ready": not page.locator("#start").is_disabled(),
                                    "status": page.locator("#status").inner_text()}
                    page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (output / "sandbox-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("sandbox diagnosed; served paged player and sandbox standalone ready")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.source, args.output)
