"""AQR2 transfer, encrypted containers, browser preparation and screen capture."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .interfaces import State
from .protocol import MAX_PACKET, ProtocolError, TransferDescriptor, decode
from .simulator import replay, schedule
from .stream import Session, read_stream, write_stream
from .transfer import packets, prepare_object


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # Unknown options might contain a mistakenly supplied password.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help.\n")


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def run_receive(args, resume=False):
    with Session(args.state, resume=resume) as session:
        try:
            with args.stream.open("rb") as source:
                for raw in read_stream(source):
                    session.receiver.feed(raw)
        except KeyboardInterrupt:
            session.receiver.cancel()
            return 130
        except (OSError, ValueError):
            session.receiver.state = State.ERROR
            raise
        snapshot = session.receiver.snapshot()
        print(json.dumps(snapshot, indent=2))
        return 0 if snapshot["content_verified"] else 2


def main(argv=None):
    parser = SafeParser(description="AQR2 visual transfer / encrypted containers / browser player")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Packetize an opaque object; does not encrypt it")
    prepare.add_argument("object", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--chunk-size", type=positive, default=2800)
    prepare.add_argument("--cycles", type=positive, default=1)
    prepare.add_argument("--descriptor", type=Path, help="Verified technical descriptor from pack (or receiver status.json)")
    render = sub.add_parser("render", help="Build an offline browser player from an encrypted object")
    render.add_argument("archive", type=Path)
    render.add_argument("--descriptor", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--generator", choices=("segno", "qrcode"), default="segno")
    render.add_argument("--metadata-every", type=positive, default=8)
    render.add_argument("--interval-ms", type=positive, default=600)
    render.add_argument("--external-only", action="store_true")
    render.add_argument("--estimate", action="store_true", help="Show size budget without generating frames")
    sub.add_parser("monitors", help="List capture monitor numbers")
    capture = sub.add_parser("capture", help="Receive one AQR2 QR from a host monitor")
    capture.add_argument("--state", type=Path, required=True)
    capture.add_argument("--monitor", type=positive, default=1)
    capture.add_argument("--fps", type=float, default=12)
    capture.add_argument("--first-timeout", type=float, default=120)
    capture.add_argument("--idle-timeout", type=float, default=600)
    capture.add_argument("--total-timeout", type=float, default=0, help="0 means no overall timeout")
    capture.add_argument("--extract-to", type=Path, help="After verification, prompt for password and extract")
    pack = sub.add_parser("pack", help="Create an encrypted 7z object; hidden password prompt")
    pack.add_argument("source", type=Path)
    pack.add_argument("--output", type=Path, required=True, help="New directory for object.7z and descriptor.json")
    pack.add_argument("--profile", choices=("strong", "max", "ultra"), default="max")
    for name in ("unpack", "inspect-container"):
        command = sub.add_parser(name, help="Verify the object before opening its encrypted contents")
        command.add_argument("archive", type=Path)
        command.add_argument("--descriptor", type=Path, required=True)
        if name == "unpack":
            command.add_argument("--output", type=Path, required=True, help="New result directory")
            command.add_argument("--attempts", type=positive, default=3)
    for name in ("receive", "resume"):
        command = sub.add_parser(name, help="Consume a local .aqs packet stream")
        command.add_argument("stream", type=Path)
        command.add_argument("--state", type=Path, required=True)
    inspect = sub.add_parser("inspect", help="Read saved status (does not reverify files)")
    inspect.add_argument("state", type=Path)
    packet = sub.add_parser("inspect-packet", help="Inspect one raw packet without printing payload")
    packet.add_argument("packet", type=Path)
    packet.add_argument("--legacy", action="store_true", help="Explicit AQR1: unencrypted legacy data")
    sim = sub.add_parser("simulate", help="Build/replay a fixed open-loop impairment schedule")
    sim.add_argument("stream", type=Path)
    sim.add_argument("--output", type=Path, required=True)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--loss", type=float, default=0.1)
    sim.add_argument("--duplicate", type=float, default=0.1)
    sim.add_argument("--corrupt", type=float, default=0.05)
    sim.add_argument("--late", type=int, default=0)
    sim.add_argument("--burst-start", type=int, default=0)
    sim.add_argument("--burst-length", type=int, default=0)
    sim.add_argument("--no-reorder", action="store_true")
    sim.add_argument("--schedule", type=Path, help="Replay a previously saved schedule")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            from .player import estimate, render
            descriptor = read_descriptor(args.descriptor)
            budget = estimate(descriptor, metadata_every=args.metadata_every, interval_ms=args.interval_ms)
            print(json.dumps(budget), flush=True)
            if args.estimate:
                return 0
            result = render(args.archive, descriptor, args.output, generator=args.generator,
                            metadata_every=args.metadata_every, interval_ms=args.interval_ms,
                            standalone=not args.external_only,
                            progress=lambda done, total: print(f"QR: {done}/{total}", file=sys.stderr, flush=True))
            print(json.dumps(result))
            return 0
        if args.command == "monitors":
            from .capture import monitors
            print(json.dumps(monitors(), indent=2))
            return 0
        if args.command == "capture":
            from .capture import receive_screen
            def progress(value):
                total = value["descriptor"]["total"] if value["descriptor"] else "?"
                print(f"{value['state']}: {value['received_chunks']}/{total}; "
                      f"{value['useful_bytes_per_second']:.0f} B/s; {value['capture_fps']:.1f} captures/s; "
                      f"{value['counters']}", file=sys.stderr, flush=True)
            result = receive_screen(args.state, monitor=args.monitor, fps=args.fps,
                                    first_timeout=args.first_timeout, idle_timeout=args.idle_timeout,
                                    total_timeout=args.total_timeout, progress=progress)
            print(json.dumps(result))
            if result["exit_code"] == 0 and args.extract_to:
                unpack_code = main(["unpack", str(args.state / "object.bin"), "--descriptor", str(args.state / "status.json"),
                                    "--output", str(args.extract_to)])
                code = unpack_code if unpack_code in (0,130) else 3
                receipt = {"schema":1, "stage":"complete" if code == 0 else "extraction_failed", "exit_code":code}
                (args.state / "extraction.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
                return code
            return result["exit_code"]
        if args.command in ("pack", "unpack", "inspect-container"):
            from . import container
            if args.command == "pack":
                if args.output.absolute().is_relative_to(args.source.absolute()):
                    raise container.ContainerError("output_inside_source")
                password = container.password_input(confirm=True)
                args.output.mkdir(parents=True, exist_ok=False)
                try:
                    archive = container.prepare(args.source, args.output / "object.7z", password, profile=args.profile)
                finally:
                    password = None
                descriptor = prepare_object(archive, container="7z-aes256").descriptor
                with (args.output / "descriptor.json").open("xb") as target:
                    target.write(descriptor.to_bytes())
                print(json.dumps({"state": "prepared", "descriptor": asdict(descriptor)}))
                return 0
            descriptor = read_descriptor(args.descriptor)
            public = container.inspect_container(args.archive, descriptor)
            if args.command == "inspect-container":
                print(json.dumps(public))
                return 0
            for attempt in range(args.attempts):
                password = container.password_input()
                try:
                    container.extract(args.archive, args.output, password, descriptor=descriptor)
                    print(json.dumps({"state": "complete", "content_verified": True, "manifest_verified": True}))
                    return 0
                except container.ContainerError as error:
                    if error.reason != "password_or_damaged_container" or attempt + 1 == args.attempts:
                        raise
                    print("Password incorrect or container damaged; retry password.", file=sys.stderr)
                finally:
                    password = None
        if args.command == "prepare":
            container_name = "opaque-test"
            if args.descriptor:
                from .container import verify_object
                descriptor = read_descriptor(args.descriptor)
                verify_object(args.object, descriptor)
                container_name = descriptor.container
            transfer = prepare_object(args.object, chunk_size=args.chunk_size, container=container_name)
            write_stream(args.output, packets(transfer, cycles=args.cycles))
            print(json.dumps({"transfer_id": transfer.transfer_id.hex(), "descriptor": asdict(transfer.descriptor)}))
            return 0
        if args.command in ("receive", "resume"):
            return run_receive(args, resume=args.command == "resume")
        if args.command == "inspect":
            print((args.state / "status.json").read_text(encoding="utf-8"))
            return 0
        if args.command == "inspect-packet":
            with args.packet.open("rb") as source:
                raw = source.read(65559 if args.legacy else MAX_PACKET + 1)
            if args.legacy:
                from .legacy import parse_packet
                result = parse_packet(raw)
                result["file_id"] = result["file_id"].hex()
                result["payload_bytes"] = len(result.pop("payload"))
                result["legacy_unencrypted"] = True
            else:
                result = asdict(decode(raw))
                result["transfer_id"] = result["transfer_id"].hex()
                result["payload_bytes"] = len(result.pop("payload"))
            print(json.dumps(result))
            return 0
        # The schedule is materialized once, before the receiver is created.
        # Bound this in-memory test bench separately from the streaming core.
        if args.stream.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Simulator input limit is 32 MiB")
        with args.stream.open("rb") as source:
            sequence = list(read_stream(source, max_records=100_000))
        if args.schedule:
            if args.schedule.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Schedule limit is 32 MiB")
            plan = json.loads(args.schedule.read_text(encoding="utf-8"))
        else:
            plan = schedule(len(sequence), seed=args.seed, loss=args.loss, duplicate=args.duplicate,
                            corrupt=args.corrupt, late=args.late, burst_start=args.burst_start,
                            burst_length=args.burst_length, reorder=not args.no_reorder)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "schedule.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        write_stream(args.output / "delivered.aqs", replay(sequence, plan))
        return run_receive(argparse.Namespace(stream=args.output / "delivered.aqs", state=args.output / "state"))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, IndexError, ImportError) as error:
        # Don't print raw bytes, metadata, source names, or exception reprs.
        reason = getattr(error, "reason", type(error).__name__)
        print(f"Failed: {reason}", file=sys.stderr)
        return 1


def read_descriptor(path):
    with path.open("rb") as source:
        raw = source.read(16385)
    if len(raw) > 16384:
        raise ProtocolError("descriptor_limit")
    obj = json.loads(raw)
    if isinstance(obj, dict) and "descriptor" in obj:
        raw = json.dumps(obj["descriptor"]).encode("utf-8")
    return TransferDescriptor.from_bytes(raw)


if __name__ == "__main__":
    raise SystemExit(main())
