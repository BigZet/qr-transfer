"""Single-monitor/single-QR capture adapter. No screenshots persisted."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

from .interfaces import State
from .protocol import META, TransferDescriptor, decode, ProtocolError
from .stream import Session


def validate_options(monitor, fps, first_timeout, idle_timeout, total_timeout):
    if type(monitor) is not int or monitor < 1 or not math.isfinite(fps) or not 0 < fps <= 60:
        raise ValueError("Invalid monitor/fps")
    if any(not math.isfinite(value) or value <= 0 for value in (first_timeout, idle_timeout)):
        raise ValueError("Invalid timeout")
    if not math.isfinite(total_timeout) or total_timeout < 0:
        raise ValueError("Invalid total timeout")


def receive_frames(directory: Path, grab, decoder, *, fps=12, first_timeout=120,
                   idle_timeout=600, total_timeout=0, clock=time.monotonic,
                   sleep=time.sleep, progress=None):
    validate_options(1, fps, first_timeout, idle_timeout, total_timeout)
    started = clock()
    frames = no_qr = 0
    seen = False
    last_new = last_report = started
    reason, code = "error", 1
    with Session(directory) as session:
        receiver = session.receiver
        try:
            while True:
                now = clock()
                if total_timeout and now - started >= total_timeout:
                    reason, code = "total_timeout", 2
                    break
                if not seen and now - started >= first_timeout:
                    reason, code = "first_timeout", 2
                    break
                if seen and now - last_new >= idle_timeout:
                    reason, code = "idle_timeout", 2
                    break
                frame_start = now
                raw = decoder(grab())
                frames += 1
                if raw is None:
                    no_qr += 1
                else:
                    # Screen MVP only accepts encrypted-container sessions.
                    try:
                        packet = decode(raw)
                        if packet.kind == META and TransferDescriptor.from_bytes(packet.payload).container != "7z-aes256":
                            receiver.counters["wrong_container"] += 1
                            raw = None
                    except ProtocolError:
                        pass  # Receiver counts the precise parser rejection below.
                    if raw is not None:
                        event = receiver.feed(raw)
                        if event.reason in ("buffered", "metadata", "accepted"):
                            if not seen or event.reason in ("buffered", "accepted"):
                                last_new = clock()
                            seen = True
                if receiver.state == State.VERIFIED:
                    reason, code = "object_verified", 0
                    break
                if receiver.state == State.ERROR:
                    reason, code = "integrity_error", 1
                    break
                if progress and clock() - last_report >= 1:
                    elapsed = max(.001, clock() - started)
                    progress({**receiver.snapshot(), "elapsed_seconds": elapsed,
                              "capture_fps": frames / elapsed, "useful_bytes_per_second": receiver.received_bytes / elapsed})
                    last_report = clock()
                sleep(max(0, 1 / fps - (clock() - frame_start)))
        except KeyboardInterrupt:
            receiver.cancel()
            reason, code = "interrupted", 130
        except Exception:
            receiver.state = State.ERROR
            reason, code = "capture_error", 1
        finally:
            elapsed = max(.000001, clock() - started)
            result = {"schema": 1, "termination": reason, "exit_code": code,
                      "elapsed_seconds": elapsed, "frames_captured": frames, "no_qr_frames": no_qr,
                      "capture_fps": frames / elapsed, "target_fps": fps,
                      "useful_bytes_per_second": receiver.received_bytes / elapsed,
                      "first_timeout": first_timeout, "idle_timeout": idle_timeout, "total_timeout": total_timeout,
                      "transfer": receiver.snapshot()}
            (directory / "capture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def monitors():
    import mss
    with mss.mss() as screen:
        return [{"monitor": i, **{key:item[key] for key in ("left","top","width","height")}}
                for i, item in enumerate(screen.monitors) if i]


def receive_screen(directory: Path, *, monitor=1, fps=12, first_timeout=120,
                   idle_timeout=600, total_timeout=0, progress=None):
    validate_options(monitor, fps, first_timeout, idle_timeout, total_timeout)
    import mss
    from PIL import Image
    import zxingcpp
    with mss.mss() as screen:
        if monitor >= len(screen.monitors):
            raise ValueError("Monitor unavailable; use monitors command")
        area = {key:screen.monitors[monitor][key] for key in ("left","top","width","height")}
        def decoder(shot):
            picture = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            value = zxingcpp.read_barcode(picture, formats=zxingcpp.BarcodeFormat.QRCode)
            return bytes(value.bytes) if value is not None else None
        result = receive_frames(directory, lambda: screen.grab(area), decoder, fps=fps,
                                first_timeout=first_timeout, idle_timeout=idle_timeout,
                                total_timeout=total_timeout, progress=progress)
        result.update(monitor=monitor, capture_area=dict(area), evidence_level="screen_capture_route_unverified")
        (directory / "capture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
