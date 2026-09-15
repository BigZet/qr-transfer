"""Experimental, axis-aligned RGB QR planes with explicit calibration patches.

Only visual decoding lives here. AQR CRC/session/transport validation remains in
the receiver. No inferred file content is used to calibrate colors.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import time

from .pipeline import Decoded

SERVICE = (b'QRC1{"family":"rgb-planes-v1","profiles":["rg4","rgb8"],'
           b'"qr":40,"ecc":"L","quiet":4,"tile":[185,195],'
           b'"patches":[4,187,22,18,6],"order":"RGB","dark":0}')


def layers_for(visual):
    try:
        return {"mono": 1, "rg4": 2, "rgb8": 3}[visual]
    except KeyError:
        raise ValueError("Unknown visual profile") from None


def schedule(data_count, every, layers, slots):
    """Packet references per tile; -1 is a monochrome visual bootstrap."""
    references = ([-1] if layers > 1 else []) + [0]
    for index in range(1, data_count + 1):
        references.append(index)
        if index % every == 0:
            references.append(0)
            if layers > 1:
                references.append(-1)
    tiles, group = [], []
    for index in references:
        if layers > 1 and index <= 0:
            if group:
                tiles.append(group + [0] * (layers - len(group)))
                group = []
            tiles.append([index] * layers)
        else:
            group.append(index)
            if len(group) == layers:
                tiles.append(group)
                group = []
    if group:
        tiles.append(group + [0] * (layers - len(group)))
    while len(tiles) % slots:
        tiles.append([0] * layers)
    return tiles


class RGBDecoder:
    """At most two axis-aligned tiles, three layers, no growing frame history.

    Acquisition uses full-size threshold planes (including periodic monochrome
    service frames). Once a QR locates a tile, central module samples and explicit
    patches are re-read on EVERY image. Bad patches never reuse stale calibration.
    """

    def __init__(self, visual="rgb8", *, method="palette", cached=True,
                 skip_unchanged=False, search_seconds=1):
        self.layers = layers_for(visual)
        if self.layers == 1 or method not in ("threshold", "palette"):
            raise ValueError("Invalid RGB decoder profile/method")
        self.method, self.cached = method, cached
        self.skip_unchanged, self.search_seconds = skip_unchanged, search_seconds
        self.boxes, self.size, self.last_search = [], None, 0
        self.fingerprints = {}
        self.calibration = {}
        self.stats = Counter()

    def __call__(self, picture):
        import numpy as np
        import zxingcpp

        began = time.perf_counter()
        self.calibration = {}
        rgb = np.asarray(picture.convert("RGB"))
        size = picture.size
        if size != self.size:
            self.boxes, self.fingerprints = [], {}
            self.size = size
            self.stats["geometry_resets"] += 1
        result, found = [], []

        def read(plane):
            return zxingcpp.read_barcodes(plane, formats=zxingcpp.BarcodeFormat.QRCode)

        def append(code, slot, layer, elapsed):
            raw = bytes(code.bytes)
            if raw == SERVICE:
                self.stats["service_symbols"] += 1
                return
            if raw.startswith(b"AQR2"):
                result.append(Decoded(raw, slot, elapsed, layer))
                self.stats[f"layer_{layer}_symbols"] += 1

        # Periodic discovery also recovers a moved tile without trusting old bounds.
        if not self.cached or not self.boxes or began-self.last_search >= self.search_seconds:
            self.stats["full_search_frames"] += 1
            for layer in range(self.layers):
                codes = read(np.where(rgb[:, :, layer] < 128, 0, 255).astype("uint8"))
                for code in codes:
                    if not (bytes(code.bytes).startswith(b"AQR2") or bytes(code.bytes) == SERVICE):
                        continue
                    p = code.position
                    # Screen profiles are upright. Reject rotated/skewed geometry.
                    x, y = p.top_left.x, p.top_left.y
                    width, height = p.top_right.x-x, p.bottom_left.y-y
                    if (width <= 0 or height <= 0 or abs(width-height) > max(3, width*.015)
                            or abs(p.top_right.y-y) > 3 or abs(p.bottom_left.x-x) > 3):
                        continue
                    box = (x, y, width/177, height/177)
                    if not any(abs(x-b[0])+abs(y-b[1]) < 12 for b in found):
                        found.append(box)
                # Raw discovery results are deliberately not returned: stable slot
                # numbering and module-center decoding below serve both methods.
            self.boxes = sorted(found)[:2]
            self.last_search = began
            self.fingerprints = {}  # Bound cache to the current geometry.

        for slot, (x, y, mx, my) in enumerate(self.boxes):
            t0 = time.perf_counter()
            xs = np.floor(x+(np.arange(177)+.5)*mx).astype(int)
            ys = np.floor(y+(np.arange(177)+.5)*my).astype(int)
            if xs.min() < 0 or ys.min() < 0 or xs.max() >= size[0] or ys.max() >= size[1]:
                continue
            samples = rgb[ys[:, None], xs[None, :]].astype(np.float32)
            if self.method == "palette":
                centers = []
                for index in range(1 << self.layers):
                    # Patch center: full tile origin is four modules above/left.
                    cx, cy = round(x+(9+22*index)*mx), round(y+186*my)
                    radius = max(1, int(min(mx, my)))
                    if cx-radius < 0 or cy-radius < 0 or cx+radius >= size[0] or cy+radius >= size[1]:
                        break
                    centers.append(np.median(rgb[cy-radius:cy+radius+1, cx-radius:cx+radius+1], axis=(0, 1)))
                if len(centers) != 1 << self.layers:
                    self.stats["calibration_missing"] += 1
                    continue
                centers = np.array(centers, dtype=np.float32)
                distances = np.sqrt(((centers[:, None]-centers[None, :])**2).sum(axis=2))
                np.fill_diagonal(distances, np.inf)
                if distances.min() < 60:
                    self.stats["calibration_rejected"] += 1
                    continue
                labels = ((samples[:, :, None]-centers)**2).sum(axis=3).argmin(axis=2)
                bits = [(labels & (1 << layer)) != 0 for layer in range(self.layers)]
                self.stats["calibrated_tiles"] += 1
                self.stats["min_palette_distance"] = float(distances.min())
                self.calibration[str(slot)] = {"image_size": list(size),
                    "qr_origin": [x, y], "module_pixels": [mx, my],
                    "centers_rgb": centers.tolist(), "min_distance": float(distances.min())}
            else:
                bits = [samples[:, :, layer] < 128 for layer in range(self.layers)]
            self.stats["split_calibration_seconds"] += time.perf_counter()-t0
            for layer, dark in enumerate(bits):
                key = (slot, layer)
                fingerprint = hashlib.blake2s(dark.tobytes()).digest() if self.skip_unchanged else None
                if fingerprint is not None and self.fingerprints.get(key) == fingerprint:
                    self.stats[f"layer_{layer}_unchanged"] += 1
                    continue
                plane = np.full((185, 185), 255, dtype=np.uint8)
                plane[4:181, 4:181] = np.where(dark, 0, 255)
                plane = np.repeat(np.repeat(plane, 3, axis=0), 3, axis=1)
                before = time.perf_counter()
                codes = read(plane)
                elapsed = time.perf_counter()-before
                self.stats[f"layer_{layer}_decode_seconds"] += elapsed
                self.stats[f"layer_{layer}_attempts"] += 1
                # Cache only successful recognized QR. Corruption must remain retryable.
                if any(bytes(c.bytes).startswith(b"AQR2") or bytes(c.bytes) == SERVICE for c in codes):
                    self.fingerprints[key] = fingerprint
                for code in codes:
                    append(code, slot, layer, elapsed)
        self.stats["decode_seconds"] += time.perf_counter()-began
        return result
