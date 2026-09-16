"""Create/verify deterministic incompressible test data, without sharing originals."""
import argparse
import hashlib
import json
from pathlib import Path


def blocks(size):
    for index, offset in enumerate(range(0, size, 1 << 20)):
        yield hashlib.shake_256(f"paged-vdi-v1:{index}".encode("ascii")).digest(min(1 << 20, size-offset))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--mib", type=int, choices=(1,10,32,60), default=10)
    args = parser.parse_args()
    size, expected = args.mib << 20, hashlib.sha256()
    if args.action == "create":
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open("xb") as target:
            for block in blocks(size):
                expected.update(block); target.write(block)
    else:
        for block in blocks(size):
            expected.update(block)
        actual = hashlib.sha256()
        with args.file.open("rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                actual.update(block)
        if args.file.stat().st_size != size or actual.digest() != expected.digest():
            print(json.dumps({"verified":False, "expected_bytes":size}))
            return 1
    result = {"action":args.action, "bytes":size, "sha256":expected.hexdigest()}
    if args.action == "verify":
        result["verified"] = True
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
