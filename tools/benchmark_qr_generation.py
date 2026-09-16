"""Compare installed QR encoders and bounded worker counts on identical packets."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_transfer.generation import encoded_frames


def run(count, output):
    import numpy as np
    import zxingcpp
    packets = [hashlib.shake_256(f"qr-benchmark:{i}".encode()).digest(2844) for i in range(count)]
    rows = []
    for encoder in ("segno", "qrcode"):
        reference = None
        for workers in (1, 2, 4):
            t0 = time.perf_counter()
            frames = list(encoded_frames(packets, encoder, workers))
            seconds = time.perf_counter()-t0
            if reference is not None:
                assert frames == reference
            reference = frames
            for raw, frame in zip(packets, frames):
                dark = np.unpackbits(np.frombuffer(frame, dtype=np.uint8))[:177*177].reshape(177,177)
                image = np.pad(np.where(dark,0,255).astype('uint8'),4,constant_values=255)
                image = np.repeat(np.repeat(image,5,axis=0),5,axis=1)
                codes = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
                assert len(codes) == 1 and bytes(codes[0].bytes) == raw, (encoder, workers, len(codes), len(codes[0].bytes) if codes else 0)
            row = {"encoder":encoder,"workers":workers,"frames":count,"seconds":seconds,
                   "frames_per_second":count/seconds,"all_payloads_exact":True}
            rows.append(row)
            print(json.dumps(row),flush=True)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({"scope":"local Windows CPU; startup included, no VDI", "runs":rows},indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames",type=int,default=64)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    if not 1 <= args.frames <= 1024:
        parser.error("frames must be 1..1024")
    run(args.frames,args.output)
