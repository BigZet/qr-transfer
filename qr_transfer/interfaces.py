"""Adapters deliberately separate packet codecs from native file transports."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol

from .protocol import TransferDescriptor


class State(str, Enum):
    WAITING = "waiting"
    BUFFERING = "buffering"
    RECEIVING = "receiving"
    ASSEMBLING = "assembling"
    VERIFYING = "verifying"
    VERIFIED = "object_verified"
    WAITING_PASSWORD = "waiting_password"
    EXTRACTING = "extracting"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class VisualProfile:
    id: str = "qr-v40-l"
    max_packet_bytes: int = 2953
    layers: int = 1


@dataclass(frozen=True)
class PreparedTransfer:
    object_path: Path
    transfer_id: bytes
    descriptor: TransferDescriptor


@dataclass(frozen=True)
class DecodeEvent:
    reason: str
    state: str
    received_chunks: int
    total_chunks: int | None
    schema: int = 1


class TransferState(Protocol):
    def feed(self, raw: bytes) -> DecodeEvent: ...
    def snapshot(self) -> dict: ...
    def cancel(self) -> None: ...


class PacketDecoder(Protocol):
    def decode(self, frame: object) -> Iterable[bytes]: ...


class FileTransport(Protocol):
    """cimbar owns its framing/FEC; never pretend its symbols are AQR packets."""
    def feed_frame(self, frame: object) -> DecodeEvent: ...
    def snapshot(self) -> dict: ...
    def verified_object(self) -> Path | None: ...


class ContainerAdapter(Protocol):
    # Password is obtained interactively by the caller; never a CLI argument.
    def prepare(self, source: Path, destination: Path, password: str) -> Path: ...
    def extract(self, archive: Path, destination: Path, password: str,
                *, descriptor: TransferDescriptor) -> Path: ...
