"""Bounded latest-frame capture and multi-QR decoding with periodic full search."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import queue
import threading
import time


@dataclass
class Decoded:
    raw: bytes
    slot: int = 0
    seconds: float = 0.0


def region(monitor, roi=None):
    """ROI is x,y,w,h relative to monitor in physical capture pixels."""
    x, y, w, h = roi if roi is not None else (0, 0, monitor['width'], monitor['height'])
    if any(type(v) is not int for v in (x,y,w,h)) or min(x,y) < 0 or min(w,h) <= 0 or x+w > monitor['width'] or y+h > monitor['height']:
        raise ValueError('roi_outside_monitor')
    return dict(left=monitor['left']+x, top=monitor['top']+y, width=w, height=h)


class LatestFrames:
    def __init__(self, factory, fps=12, capacity=2):
        if capacity not in (1,2):
            raise ValueError('queue_capacity')
        self.factory, self.fps = factory, fps
        self.queue = queue.Queue(capacity)
        self.stop = threading.Event()
        self.error = None
        self.stats = Counter(captured=0, dropped_old=0, queue_peak=0, capture_seconds=0.0)
        self.thread = None

    def _run(self):
        try:
            with self.factory() as grab:
                deadline = time.monotonic()
                while not self.stop.is_set():
                    before = time.monotonic(); frame = grab()
                    self.stats['capture_seconds'] += time.monotonic()-before
                    self.stats['captured'] += 1
                    try:
                        self.queue.put_nowait(frame)
                    except queue.Full:
                        try:
                            self.queue.get_nowait(); self.stats['dropped_old'] += 1
                        except queue.Empty:
                            pass
                        self.queue.put_nowait(frame)
                    self.stats['queue_peak'] = max(self.stats['queue_peak'], self.queue.qsize())
                    deadline = max(deadline+1/self.fps, time.monotonic())
                    self.stop.wait(max(0, deadline-time.monotonic()))
        except BaseException as error:
            self.error = error

    def grab(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name='screen-capture')
            self.thread.start()
        if self.error:
            raise RuntimeError('capture_worker_failed') from self.error
        try:
            return self.queue.get(timeout=.1)
        except queue.Empty:
            return None

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.thread and self.thread.is_alive():
            raise RuntimeError('capture_worker_stuck')


class MultiDecoder:
    def __init__(self, *, cached=True, search_seconds=1):
        self.boxes = []
        self.size = None
        self.last_search = 0
        self.cached, self.search_seconds = cached, search_seconds
        self.stats = Counter()

    def __call__(self, picture):
        import zxingcpp
        start = time.monotonic()
        def read(image):
            return zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
        if self.cached and self.boxes and self.size == picture.size and start-self.last_search < self.search_seconds:
            result = []
            complete = True
            for slot, box in enumerate(self.boxes):
                before = time.monotonic(); codes = read(picture.crop(box)); elapsed = time.monotonic()-before
                complete = complete and len(codes) == 1
                result.extend(Decoded(bytes(c.bytes), slot, elapsed/max(1,len(codes))) for c in codes)
            if complete:
                self.stats['cached_frames'] += 1
                self.stats['decode_seconds'] += time.monotonic()-start
                return result
        codes = sorted(read(picture), key=lambda c: c.position.top_left.x)
        self.boxes = []
        for c in codes[:2]:
            points = [c.position.top_left,c.position.top_right,c.position.bottom_left,c.position.bottom_right]
            x0,y0 = min(p.x for p in points), min(p.y for p in points)
            x1,y1 = max(p.x for p in points), max(p.y for p in points)
            pad = max(12, int(max(x1-x0,y1-y0)*.12))
            self.boxes.append((max(0,x0-pad),max(0,y0-pad),min(picture.width,x1+pad),min(picture.height,y1+pad)))
        # Cache only exactly one/two results. Unexpected extra codes trigger full search.
        if len(codes) > 2:
            self.boxes=[]
        self.size, self.last_search = picture.size, start
        elapsed = time.monotonic()-start
        self.stats['full_search_frames'] += 1
        self.stats['decode_seconds'] += elapsed
        return [Decoded(bytes(c.bytes), slot, elapsed/max(1,len(codes))) for slot,c in enumerate(codes)]


@contextmanager
def screen_source(monitor, roi, diagnostics=None):
    import mss
    from PIL import Image
    with mss.mss() as screen:
        refreshed = [0.0]
        def grab():
            # Refresh geometry on each capture; fixed ROI must still fit the monitor.
            if time.monotonic()-refreshed[0] >= 1:
                screen._monitors = None  # mss 10.2.0 topology cache.
                refreshed[0] = time.monotonic()
            if monitor >= len(screen.monitors):
                raise ValueError('monitor_unavailable')
            try:
                area = region(screen.monitors[monitor], roi)
                fallback = False
            except ValueError:
                area = region(screen.monitors[monitor])
                fallback = True
            if diagnostics is not None:
                diagnostics.update(last_area=area, roi_fallback_full_monitor=fallback)
            shot = screen.grab(area)
            return Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
        yield grab
