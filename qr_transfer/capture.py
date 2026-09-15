"""Screen capture orchestration with transactional resume and bounded frames."""
from __future__ import annotations

import json
import math
from collections import defaultdict, Counter
from pathlib import Path
import time

from .interfaces import State
from .protocol import META, TransferDescriptor, decode, ProtocolError
from .storage import DurableSession
from .pipeline import Decoded, LatestFrames, MultiDecoder, region, screen_source


def validate_options(monitor, fps, first_timeout, idle_timeout, total_timeout):
    if type(monitor) is not int or monitor < 1 or not math.isfinite(fps) or not 0 < fps <= 60:
        raise ValueError("Invalid monitor/fps")
    if any(not math.isfinite(value) or value <= 0 for value in (first_timeout, idle_timeout)):
        raise ValueError("Invalid timeout")
    if not math.isfinite(total_timeout) or total_timeout < 0:
        raise ValueError("Invalid total timeout")


def receive_frames(directory: Path, grab, decoder, *, fps=12, first_timeout=120,
                   idle_timeout=600, total_timeout=0, clock=time.monotonic,
                   sleep=time.sleep, progress=None, resume=False, paced=False, transport="repeat"):
    validate_options(1, fps, first_timeout, idle_timeout, total_timeout)
    started = clock()
    frames = no_qr = 0
    seen = False
    last_new = last_report = started
    reason, code = "error", 1
    slots = defaultdict(Counter)
    layers = defaultdict(Counter)
    session_type = DurableSession
    packet_decoder = decode
    if transport == "lt":
        from .fec import FecSession, decode as fec_decode
        session_type, packet_decoder = FecSession, fec_decode
    elif transport != "repeat":
        raise ValueError("Unknown transport")
    with session_type(directory, resume=resume) as session:
        receiver = session.receiver
        initial_bytes = receiver.received_bytes
        decode_seconds = 0.0
        try:
            import psutil
            process = psutil.Process()
        except ImportError:
            process = None
        peak_rss = process.memory_info().rss if process else None
        try:
            while True:
                if receiver.state == State.VERIFIED:
                    reason, code = "object_verified", 0
                    break
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
                image = grab()
                before = clock()
                decoded = decoder(image) if image is not None else []
                decode_seconds += clock() - before
                frames += image is not None
                if decoded is None:
                    decoded = []
                if isinstance(decoded, bytes):
                    decoded = [Decoded(decoded)]
                if not decoded:
                    no_qr += image is not None
                for item in decoded:
                    if isinstance(item, bytes):
                        item = Decoded(item)
                    raw = item.raw
                    metrics = slots[item.slot] if item.layer is None else layers[f'{item.slot}:{item.layer}']
                    metrics['symbols'] += 1
                    metrics['decode_seconds'] += item.seconds
                    try:
                        packet = packet_decoder(raw)
                        if transport == "repeat" and packet.kind == META and TransferDescriptor.from_bytes(packet.payload).container != '7z-aes256':
                            receiver.counters['wrong_container'] += 1
                            metrics['invalid'] += 1
                            continue
                    except ProtocolError:
                        metrics['invalid'] += 1
                    else:
                        metrics['valid'] += 1
                    previous_bytes = receiver.received_bytes
                    event = receiver.feed(raw)
                    metrics["restored_bytes_delta"] += max(0, receiver.received_bytes-previous_bytes)
                    metrics[event.reason] += 1
                    if event.reason == "accepted":
                        metrics["accepted_payload_bytes"] += len(packet.payload)
                    if event.reason == 'conflict':
                        receiver.state = State.ERROR
                        reason, code = 'packet_conflict', 1
                        break
                    if event.reason in ('buffered', 'metadata', 'accepted'):
                        if not seen or event.reason in ('buffered', 'accepted'):
                            last_new = clock()
                        seen = True
                if receiver.state == State.VERIFIED:
                    reason, code = "object_verified", 0
                    break
                if receiver.state == State.ERROR:
                    reason, code = ("packet_conflict" if reason == "packet_conflict" else "integrity_error"), 1
                    break
                if clock() - last_report >= 1:
                    if process:
                        peak_rss = max(peak_rss, process.memory_info().rss)
                    elapsed = max(.001, clock() - started)
                    session.write_status()
                    status = {**session.snapshot(), "elapsed_seconds": elapsed,
                              "capture_fps": frames / elapsed, "useful_bytes_per_second": (receiver.received_bytes - initial_bytes) / elapsed,
                              "no_progress_seconds":clock()-last_new, "rss_sampled_peak":peak_rss}
                    if progress:
                        progress(status)
                    last_report = clock()
                if not paced:
                    sleep(max(0, 1 / fps - (clock() - frame_start)))
        except KeyboardInterrupt:
            receiver.cancel()
            reason, code = "interrupted", 130
        except Exception as error:
            receiver.state = State.ERROR
            reason, code = getattr(error, "reason", "capture_error"), 1
        finally:
            elapsed = max(.000001, clock() - started)
            result = {"schema": 1, "termination": reason, "exit_code": code,
                      "elapsed_seconds": elapsed, "frames_captured": frames, "no_qr_frames": no_qr,
                      "capture_fps": frames / elapsed, "target_fps": fps,
                      "useful_bytes_per_second": (receiver.received_bytes - initial_bytes) / elapsed,
                      "first_timeout": first_timeout, "idle_timeout": idle_timeout, "total_timeout": total_timeout,
                      "rss_sampled_peak":peak_rss, "resumed":resume, "initial_bytes":initial_bytes, "decode_seconds":decode_seconds,
                      "no_progress_seconds":clock()-last_new, "slots":dict(slots), "layers":dict(layers),
                      "transfer": session.snapshot()}
            (directory / "capture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def monitors():
    import mss
    with mss.mss() as screen:
        return [{"monitor": i, **{key:item[key] for key in ("left","top","width","height")}}
                for i, item in enumerate(screen.monitors) if i]


def receive_screen(directory: Path, *, monitor=1, fps=12, first_timeout=120,
                   idle_timeout=600, total_timeout=0, progress=None, resume=False,
                   roi=None, pipeline=True, queue_size=2, cached=True, transport="repeat", visual="mono", color_method="palette", skip_unchanged=False):
    validate_options(monitor, fps, first_timeout, idle_timeout, total_timeout)
    available = monitors()
    if monitor > len(available):
        raise ValueError('monitor_unavailable')
    area = region(available[monitor-1], roi)
    if visual == "mono":
        decoder = MultiDecoder(cached=cached)
    else:
        from .rgb import RGBDecoder
        decoder = RGBDecoder(visual, method=color_method, cached=cached, skip_unchanged=skip_unchanged)
    geometry = {}
    factory = lambda: screen_source(monitor, roi, geometry)
    options = dict(fps=fps, first_timeout=first_timeout, idle_timeout=idle_timeout,
                   total_timeout=total_timeout, progress=progress, resume=resume, transport=transport)
    if pipeline:
        source = LatestFrames(factory, fps=fps, capacity=queue_size)
        try:
            result = receive_frames(directory, source.grab, decoder, paced=True, **options)
        finally:
            source.close()
        result['pipeline'] = dict(source.stats)
        result['pipeline']['capture_fps'] = source.stats['captured'] / result['elapsed_seconds']
        result['pipeline']['processed_fps'] = result['capture_fps']
    else:
        with factory() as grab:
            result = receive_frames(directory, grab, decoder, **options)
    result.update(visual=visual, color_method=color_method if visual != "mono" else None, monitor=monitor, capture_area=area, roi=roi, geometry=geometry,
                  decoder=dict(decoder.stats), color_calibration=getattr(decoder, 'calibration', None),
                  evidence_level='screen_capture_route_unverified')
    (directory / 'capture.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return result
