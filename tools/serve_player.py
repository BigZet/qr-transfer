"""Serve one player directory on loopback for the authenticated Jupyter proxy."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path


class PlayerHandler(SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")
        return None

    def send_head(self):
        target = Path(self.translate_path(self.path)).resolve()
        root = Path(self.directory).resolve()
        if not target.is_relative_to(root):
            self.send_error(403, "Outside player directory")
            return None
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        # Missing descriptors are expected while live generation is running.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_dir():
        parser.error("Player directory does not exist")
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), partial(PlayerHandler, directory=str(directory))) as server:
        prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
        if prefix:
            print("Open on the same JupyterHub host: " + prefix.rstrip("/") + f"/proxy/{args.port}/index.html", flush=True)
            print("Requires jupyter-server-proxy enabled in the running Jupyter server.", flush=True)
        else:
            print(f"Open http://127.0.0.1:{args.port}/index.html on this machine.", flush=True)
        print("Keep this terminal running. Ctrl+C stops the server.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
