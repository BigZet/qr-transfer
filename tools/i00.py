"""Iteration 00 diagnostics and baseline helpers; Python 3.10+, stdlib first.

Run from any directory: python /path/to/repo/tools/i00.py --help.
No network requests, installs or screenshot files are made by probe.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "pip": "pip", "qrcode": "qrcode", "Pillow": "PIL", "mss": "mss",
    "zxing-cpp": "zxingcpp", "psutil": "psutil", "py7zr": "py7zr",
    "segno": "segno", "numpy": "numpy", "raptorq": "raptorq",
    "jupyterhub": "jupyterhub", "jupyterlab": "jupyterlab",
    "jupyter-server": "jupyter_server", "notebook": "notebook",
    "ipykernel": "ipykernel", "jupyter-server-proxy": "jupyter_server_proxy",
}
FIXTURE_VERSION = 1
FIXTURE_SEED = "qr-transfer-i00-v1"


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, data: object) -> None:
    """Create a report without overwriting a previous run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def error_info(exc: Exception) -> dict:
    # Never copy URLs (which can contain credentials/tokens) into diagnostic output.
    detail = re.sub(r"(?:https?|file)://\S+", "<url omitted>", str(exc))
    return {"status": "error", "error_type": type(exc).__name__, "detail": detail[:500]}


def positive(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def nonnegative(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return result


def provenance(level: str) -> dict:
    files = {name: digest((ROOT / name).read_bytes()) for name in ("code.py", "qr.py", "tools/i00.py")}
    versions = {}
    for package in ("qrcode", "Pillow", "mss", "zxing-cpp", "psutil"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {"schema": 1, "created_utc": stamp(), "evidence_level": level,
            "python": platform.python_version(), "system": platform.system(),
            "machine": platform.machine(), "source_sha256": files, "package_versions": versions}


def load_legacy(name: str, filename: str):
    # code.py shadows a Python stdlib module: always load the exact repository path.
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_probe(distribution: str) -> dict:
    module_name = PACKAGES[distribution]
    try:
        metadata = importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return {"status": "missing", "distribution": distribution}
    result = {"distribution": distribution, "version": metadata.version,
              "requires_python": metadata.metadata.get("Requires-Python"),
              "installed_wheel_tags": re.findall(r"^Tag: (.+)$", metadata.read_text("WHEEL") or "", re.M),
              "mirror_availability": "not_checked"}
    try:
        __import__(module_name)
        result["status"] = "import_ok"
    except Exception as exc:
        result.update(error_info(exc))
    return result


def isolated_package_probe(name: str) -> dict:
    try:
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_package", name],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        # Imports occasionally print informational text. The final line is our JSON.
        if process.returncode:
            return {"status": "error", "error_type": "ImportProcessError", "returncode": process.returncode}
        return json.loads(process.stdout.splitlines()[-1])
    except Exception as exc:
        return error_info(exc)


def smoke_qr() -> dict:
    try:
        sender = load_legacy("i00_sender_smoke", "code.py")
        from PIL import Image
        import zxingcpp
        data = bytes(range(256)) + b"\x00\xff\x80binary\x00"
        png = sender.make_frame(data, 6)
        image = Image.open(io.BytesIO(png))
        decoded = zxingcpp.read_barcode(image)
        return {"status": "ok" if decoded and bytes(decoded.bytes) == data else "failed",
                "bytes": len(data), "image_size": list(image.size), "binary_payload": True}
    except ModuleNotFoundError:
        return {"status": "not_checked", "reason": "qrcode/Pillow/zxing-cpp required together"}
    except Exception as exc:
        return error_info(exc)


def capture_probe(monitor: int) -> dict:
    try:
        import mss
        factory = getattr(mss, "MSS", mss.mss)
        with factory() as capture:
            monitors = list(capture.monitors)
            if not 0 <= monitor < len(monitors):
                raise ValueError("Monitor index outside available range")
            durations = []
            for _ in range(3):
                start = time.perf_counter()
                shot = capture.grab(monitors[monitor])
                durations.append(time.perf_counter() - start)
            geometry = [{key: entry[key] for key in ("left", "top", "width", "height")} for entry in monitors]
            return {"status": "ok", "monitors": geometry, "selected": monitor,
                    "captured_size": list(shot.size), "grab_seconds": durations,
                    "pixels_saved": False, "vdi_color_preservation": "not_checked"}
    except Exception as exc:
        return error_info(exc)


def probe(args) -> int:
    report = provenance("environment_only")
    report.update({"declared_role": args.role, "runtime": {
        "executable": sys.executable, "version": sys.version, "platform": platform.platform(),
        "pointer_bits": 64 if sys.maxsize > 2**32 else 32, "cwd": str(Path.cwd()),
        "preferred_python_310": sys.version_info[:2] == (3, 10),
    }, "jupyter_environment_markers_present": {
        key: key in os.environ for key in ("JUPYTERHUB_SERVICE_PREFIX", "JUPYTERHUB_USER", "JPY_PARENT_PID")
    }, "manual_checks": {key: "not_checked" for key in (
        "jupyter_frontend", "launch_interface", "active_html_route", "os_scale", "browser_zoom",
        "vdi_client_scale", "vdi_codec", "vdi_color", "mirror_installation", "github_zip_delivery")}})
    report["packages"] = {name: isolated_package_probe(name) for name in PACKAGES}
    report["qr_smoke"] = smoke_qr()
    report["archive_executables"] = {name: shutil.which(name) for name in ("7z", "7zz", "7za")}
    report["disk"] = dict(zip(("total_bytes", "used_bytes", "free_bytes"), shutil.disk_usage(ROOT)))
    try:
        # Test the report destination, not an arbitrary system-wide temporary directory.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=args.output.parent) as stream:
            stream.write(b"i00-write-check")
            stream.flush()
        report["report_directory_writable"] = True
    except OSError as exc:
        report["report_directory_writable"] = error_info(exc)
    try:
        import psutil
        report["memory"] = {"physical_total_bytes": psutil.virtual_memory().total,
                            "available_bytes": psutil.virtual_memory().available,
                            "process_rss_bytes": psutil.Process().memory_info().rss}
    except ImportError:
        report["memory"] = {"status": "not_checked", "reason": "optional psutil unavailable"}
    if sys.platform != "win32":
        import resource
        report["process_limits"] = {name: list(resource.getrlimit(getattr(resource, name)))
                                    for name in ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_NOFILE")}
        report["cgroup_limits"] = {}
        for name in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                report["cgroup_limits"][name] = Path(name).read_text().strip()
            except OSError:
                pass
    else:
        report["process_limits"] = {"status": "not_checked", "reason": "Windows job limits not inferred from physical RAM"}
    if args.source:
        try:
            with args.source.open("rb") as stream:
                stream.read(1)
            report["source_access"] = {"status": "ok", "path": str(args.source.resolve()),
                                       "size_bytes": args.source.stat().st_size}
        except Exception as exc:
            report["source_access"] = error_info(exc)
    report["capture"] = capture_probe(args.monitor) if args.capture else {"status": "not_checked"}
    write_json(args.output, report)
    print(f"Environment report: {args.output}")
    print("Missing packages:", ", ".join(name for name, item in report["packages"].items() if item["status"] == "missing"))
    print("VDI, browser route and mirror installation require separate checks.")
    return 0


def deterministic_bytes(size: int, label: str) -> bytes:
    prefix = f"{FIXTURE_SEED}:{label}:".encode("ascii")
    return b"".join(hashlib.sha256(prefix + i.to_bytes(8, "big")).digest()
                    for i in range(math.ceil(size / 32)))[:size]


def file_record(data: bytes) -> dict:
    return {"size": len(data), "sha256": digest(data)}


def fixtures(args) -> int:
    if not 64 <= args.size_kib <= 256:
        raise ValueError("Baseline fixture size must be between 64 and 256 KiB")
    args.output.mkdir(parents=True, exist_ok=False)
    files = {
        "binary.bin": deterministic_bytes(args.size_kib * 1024, "binary"),
        "tree/данные/случайный.bin": deterministic_bytes(args.size_kib * 1024, "tree"),
        "tree/данные/описание.txt": "Тест QR Transfer: Unicode, пустые файлы и каталоги.\n".encode("utf-8"),
        "tree/empty.bin": b"",
    }
    for relative, data in files.items():
        path = args.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (args.output / "tree/пустой каталог").mkdir()
    # Make tar inputs reproducible as far as the original tarfile backend permits.
    for path in sorted(args.output.rglob("*"), reverse=True):
        os.utime(path, (1700000000, 1700000000))
    manifest = {"schema": FIXTURE_VERSION, "seed": FIXTURE_SEED, "size_kib": args.size_kib,
                "files": {name: file_record(data) for name, data in files.items()},
                "directories": ["tree", "tree/данные", "tree/пустой каталог"]}
    write_json(args.output / "manifest.json", manifest)
    print(f"Synthetic fixtures: {args.output}")
    return 0


def verify_fixture_set(folder: Path) -> dict:
    manifest = read_json(folder / "manifest.json")
    if manifest["schema"] != FIXTURE_VERSION or manifest["seed"] != FIXTURE_SEED:
        raise ValueError("Unknown fixture version/seed")
    for name, expected in manifest["files"].items():
        if file_record((folder / name).read_bytes()) != expected:
            raise ValueError(f"Changed fixture: {name}")
    return manifest


def verify_received(path: Path, fixture_dir: Path, case: str) -> dict:
    manifest = verify_fixture_set(fixture_dir)
    if case == "binary":
        actual = file_record(path.read_bytes())
        if actual != manifest["files"]["binary.bin"]:
            raise ValueError("Received binary differs from source fixture")
        return actual
    # Inspect only; never extract received archive paths to the filesystem in I00.
    expected = {name: item for name, item in manifest["files"].items() if name.startswith("tree/")}
    found = {}
    directories = []
    with tarfile.open(path, "r:xz") as archive:
        for member in archive:
            if member.isdir():
                directories.append(member.name.rstrip("/"))
            elif member.isfile() and member.name in expected and member.name not in found:
                if member.size != expected[member.name]["size"]:
                    raise ValueError("Archive member size differs from fixture")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Missing archive member bytes")
                with stream:
                    found[member.name] = file_record(stream.read(member.size + 1))
            else:
                raise ValueError("Unexpected/duplicate/special member in fixture archive")
    if found != expected or sorted(directories) != sorted(manifest["directories"]):
        raise ValueError("Archive tree differs from fixture manifest")
    return {"files": len(found), "directories": len(directories), "source_bytes": sum(v["size"] for v in found.values()),
            "archive": file_record(path.read_bytes()), "extracted": False}


def stop_process_tree(child: subprocess.Popen) -> None:
    """Stop only the process tree started by this runner, including venv launchers."""
    if child.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if child.poll() is None:
        child.kill()
    child.wait()


def run_process(command: list[str], log: Path, timeout: float) -> dict:
    """Measure an unmodified legacy script; keep stdout off the orchestration pipe."""
    start = time.perf_counter()
    peak = None
    reason = None
    with log.open("x", encoding="utf-8") as stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=ROOT,
                                 start_new_session=os.name != "nt")
        try:
            import psutil
            try:
                observed = psutil.Process(child.pid)
            except psutil.Error:
                observed = None
        except ImportError:
            observed = None
        try:
            while child.poll() is None:
                if observed:
                    try:
                        # Windows venv python.exe can be a launcher with a Python child.
                        processes = [observed, *observed.children(recursive=True)]
                        rss = 0
                        for process in processes:
                            try:
                                rss += process.memory_info().rss
                            except psutil.Error:
                                pass
                        peak = max(peak or 0, rss)
                    except psutil.Error:
                        pass
                if time.perf_counter() - start > timeout:
                    reason = "timeout"
                    stop_process_tree(child)
                    break
                time.sleep(0.02)
            returncode = child.wait()
        except BaseException:
            stop_process_tree(child)
            raise
    return {"returncode": returncode, "wall_seconds": time.perf_counter() - start,
            "sampled_peak_rss_bytes": peak, "rss_sampling_seconds": 0.02,
            "termination": reason, "memory_scope": "process tree RSS sum, sampled; shared pages may be counted more than once"}


def prepare(args) -> int:
    manifest = verify_fixture_set(args.fixtures)
    if args.output.resolve().is_relative_to(args.fixtures.resolve()):
        raise ValueError("Preparation output must be outside the fixture directory")
    args.output.mkdir(parents=True, exist_ok=False)
    report = provenance("legacy_preparation_only")
    report.update({"fixture": {"schema": manifest["schema"], "seed": manifest["seed"], "size_kib": manifest["size_kib"]},
                   "settings": {"interval_ms": 600, "box_size": 6, "chunk_size": 2800, "qr": "V40-L", "protocol": "AQR1"}, "cases": {}})
    for case, source in (("binary", "binary.bin"), ("tree", "tree")):
        output = args.output.resolve() / f"{case}.html"
        print(f"Preparing {case}...", flush=True)
        measured = run_process([sys.executable, str(ROOT / "code.py"), str((args.fixtures / source).resolve()),
                                "-o", str(output), "--interval", "600", "--box-size", "6", "--chunk-size", "2800"],
                               args.output / f"{case}-prepare.log", 600)
        measured["ok"] = measured["returncode"] == 0 and output.exists() and output.stat().st_size > 0
        if measured["ok"]:
            measured["html_bytes"] = output.stat().st_size
            measured["html_sha256"] = digest(output.read_bytes())
            measured["display_frames"] = len(html_frames(output))
        report["cases"][case] = measured
    write_json(args.output / "prepare.json", report)
    return 0 if all(item["ok"] for item in report["cases"].values()) else 1


def html_frames(path: Path) -> list[bytes]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"const frames\s*=\s*(\[.*?\]);", text, re.S)
    if not match:
        raise ValueError("Legacy HTML frames array not found")
    frames = json.loads(match.group(1))
    prefix = "data:image/png;base64,"
    if not frames or any(not isinstance(value, str) or not value.startswith(prefix) for value in frames):
        raise ValueError("Unexpected frame source")
    return [base64.b64decode(value[len(prefix):], validate=True) for value in frames]


def roundtrip(args) -> int:
    from PIL import Image
    import zxingcpp
    receiver = load_legacy("i00_receiver_parser", "qr.py")
    verify_fixture_set(args.fixtures)
    args.output.mkdir(parents=True, exist_ok=False)
    report = provenance("synthetic_png_roundtrip_no_browser_no_vdi")
    report["runs"] = []
    for case in ("binary", "tree"):
        frames = html_frames(args.prepared / f"{case}.html")
        for run in range(1, args.runs + 1):
            start = time.perf_counter()
            result = {"case": case, "run": run, "ok": False, "png_frames": len(frames)}
            try:
                chunks = {}
                metadata = None
                identity = None
                total = None
                for png in frames:
                    image = Image.open(io.BytesIO(png)).convert("RGB")
                    barcode = zxingcpp.read_barcode(image)
                    if barcode is None:
                        raise ValueError("PNG did not decode")
                    packet = receiver.parse_packet(bytes(barcode.bytes))
                    if packet is None:
                        raise ValueError("Decoded bytes failed legacy parser")
                    if identity is None:
                        identity, total = packet["file_id"], packet["total"]
                    if (packet["file_id"], packet["total"]) != (identity, total):
                        raise ValueError("Mixed fixture session")
                    if packet["type"] == receiver.TYPE_META:
                        new = json.loads(packet["payload"])
                        if metadata is not None and metadata != new:
                            raise ValueError("Conflicting metadata")
                        metadata = new
                    elif packet["type"] == receiver.TYPE_DATA:
                        index = packet["index"]
                        if not 1 <= index <= total or (index in chunks and chunks[index] != packet["payload"]):
                            raise ValueError("Invalid/conflicting fixture index")
                        chunks[index] = packet["payload"]
                    else:
                        raise ValueError("Unexpected fixture packet type")
                if metadata is None or len(chunks) != total:
                    raise ValueError("Incomplete fixture transfer")
                data = b"".join(chunks[index] for index in range(1, total + 1))
                if len(data) != metadata["size"] or digest(data) != metadata["sha256"]:
                    raise ValueError("Final archive length/hash mismatch")
                output = args.output / f"{case}-{run}.received"
                output.write_bytes(data)
                result.update({"ok": True, "received": verify_received(output, args.fixtures, case),
                               "transport_bytes": len(data), "unique_chunks": len(chunks)})
            except Exception as exc:
                result.update(error_info(exc))
            result["decode_verify_seconds"] = time.perf_counter() - start
            report["runs"].append(result)
            print(f"{case} {run}/{args.runs}: {'OK' if result['ok'] else 'FAILED'}", flush=True)
    write_json(args.output / "roundtrip.json", report)
    return 0 if all(item["ok"] for item in report["runs"]) else 1


def receive(args) -> int:
    verify_fixture_set(args.fixtures)
    args.output.mkdir(parents=True, exist_ok=False)
    # A new isolated directory prevents the legacy receiver overwriting old runs.
    destination = args.output.resolve() / "received"
    destination.mkdir()
    print("Start the matching baseline HTML in the VDI browser now.", flush=True)
    report = provenance(args.level)
    report.update({"case": args.case, "monitor": args.monitor, "fps_requested": args.fps,
                   "timeout_seconds": args.timeout, "capture": capture_probe(args.monitor), "ok": False,
                   "time_definition": "receiver process start to exit + independent fixture verification; includes waiting for playback"})
    try:
        report["process"] = run_process([sys.executable, str(ROOT / "qr.py"), "--monitor", str(args.monitor),
                                        "--fps", str(args.fps), "--timeout", str(args.timeout),
                                        "--output-dir", str(destination)], args.output / "receive.log", args.timeout + 30)
    except KeyboardInterrupt:
        report["process"] = {"termination": "interrupted", "returncode": None}
        write_json(args.output / "receive.json", report)
        print(f"Interrupted; report saved to {args.output / 'receive.json'}")
        return 130
    try:
        if report["process"]["returncode"] != 0 or report["process"]["termination"]:
            raise ValueError("Legacy receiver failed or exceeded wrapper timeout")
        # Legacy qr.py returns exit code 0 on timeout too: independently require output and hashes.
        path = destination / ("binary.bin" if args.case == "binary" else "tree.tar.xz")
        start = time.perf_counter()
        report["received"] = verify_received(path, args.fixtures, args.case)
        report["verification_seconds"] = time.perf_counter() - start
        report["ok"] = True
    except Exception as exc:
        report["verification"] = error_info(exc)
    write_json(args.output / "receive.json", report)
    print(f"Baseline {'OK' if report['ok'] else 'FAILED'}; see {args.output / 'receive.json'}")
    return 0 if report["ok"] else 1


def browser_bundle(args) -> int:
    source = ROOT / "diagnostics/browser"
    if args.output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Browser output must be outside the source bundle")
    shutil.copytree(source, args.output)
    print(f"Browser diagnostic bundle: {args.output / 'index.html'}")
    print("Open via your existing JupyterHub route. JavaScript/WASM policy is measured, not changed.")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    environment = commands.add_parser("probe", help="stdlib-first environment report, no network")
    environment.add_argument("--role", choices=("local", "host", "jupyterhub"), required=True)
    environment.add_argument("--output", type=Path, required=True)
    environment.add_argument("--source", type=Path)
    environment.add_argument("--capture", action="store_true", help="capture three frames in memory; save only geometry/timings")
    environment.add_argument("--monitor", type=nonnegative, default=1)
    environment.set_defaults(handler=probe)
    data = commands.add_parser("fixtures", help="create reproducible 64-256 KiB synthetic inputs")
    data.add_argument("--output", type=Path, required=True)
    data.add_argument("--size-kib", type=positive, default=64)
    data.set_defaults(handler=fixtures)
    page = commands.add_parser("browser", help="copy the complete offline browser diagnostic")
    page.add_argument("--output", type=Path, required=True)
    page.set_defaults(handler=browser_bundle)
    encode = commands.add_parser("prepare", help="run unmodified code.py on both fixtures")
    encode.add_argument("--fixtures", type=Path, required=True)
    encode.add_argument("--output", type=Path, required=True)
    encode.set_defaults(handler=prepare)
    local = commands.add_parser("roundtrip", help="decode actual PNGs from legacy HTML; not a VDI benchmark")
    local.add_argument("--fixtures", type=Path, required=True)
    local.add_argument("--prepared", type=Path, required=True)
    local.add_argument("--output", type=Path, required=True)
    local.add_argument("--runs", type=positive, default=3)
    local.set_defaults(handler=roundtrip)
    capture = commands.add_parser("receive", help="run original qr.py and independently verify baseline output")
    capture.add_argument("--fixtures", type=Path, required=True)
    capture.add_argument("--case", choices=("binary", "tree"), required=True)
    capture.add_argument("--level", choices=("local_screen", "real_vdi"), required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--monitor", type=nonnegative, default=1)
    capture.add_argument("--fps", type=positive, default=12)
    capture.add_argument("--timeout", type=positive, default=300)
    capture.set_defaults(handler=receive)
    internal = commands.add_parser("_package", help="internal isolated import check")
    internal.add_argument("name", choices=tuple(PACKAGES))
    internal.set_defaults(handler=lambda args: print(json.dumps(package_probe(args.name))) or 0)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, ImportError) as exc:
        print(json.dumps(error_info(exc), ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
