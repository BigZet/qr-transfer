"""Transactional packet store. SQLite is authoritative; chunk files are caches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3

from .protocol import Limits, MAX_PACKET, META, ProtocolError, decode, TransferDescriptor
from .transfer import Receiver

RESERVE = 16 << 20


class WriterLock:
    """OS-held byte/file lock; automatically released even after process death."""
    def __init__(self, path):
        self.file = path.open('a+b')
        try:
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            failure = FileExistsError('session_locked')
            failure.reason = 'session_locked'
            raise failure from error

    def close(self):
        self.file.close()  # Persistent inode prevents an unlink/reopen lock race.


class DurableSession:
    def __init__(self, directory: Path, *, resume=False, limits=Limits()):
        self.directory = directory
        if not resume:
            directory.mkdir(parents=True, exist_ok=False)
        elif not directory.is_dir():
            raise ValueError('session_missing')
        self.lock = WriterLock(directory / '.writer.lock')
        self.db = None
        self.committed = 0
        self.limits = limits
        try:
            path = directory / 'packets.sqlite3'
            if resume and not path.is_file():
                raise ProtocolError('durable_session_required')
            # Bound even a corrupt input before opening/replaying it.
            if path.exists() and path.stat().st_size > self.max_state_bytes:
                raise ProtocolError('state_limit')
            self.db = sqlite3.connect(path)
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA cache_size=-2048')
            if resume:
                if self.db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise ProtocolError('state_schema')
                if self.db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ProtocolError('state_corrupt')
            else:
                self.db.execute('CREATE TABLE packets (seq INTEGER PRIMARY KEY, kind INTEGER NOT NULL, idx INTEGER NOT NULL, raw BLOB NOT NULL, UNIQUE(kind,idx))')
                self.db.execute('PRAGMA user_version=1')
                self.db.commit()
            self.receiver = Receiver(directory, limits=limits)
            if resume:
                count, size, largest = self.db.execute('SELECT count(*),coalesce(sum(length(raw)),0),coalesce(max(length(raw)),0) FROM packets').fetchone()
                if count > limits.max_chunks+1 or largest > MAX_PACKET or size > limits.max_object+limits.max_chunks*44+MAX_PACKET:
                    raise ProtocolError('state_limit')
                # Never trust status.json or caches as a received-packet index.
                for count, (kind, index, raw) in enumerate(self.db.execute('SELECT kind,idx,raw FROM packets ORDER BY seq'), 1):
                    if count > limits.max_chunks + 1 or len(raw) > MAX_PACKET:
                        raise ProtocolError('state_limit')
                    packet = decode(raw)
                    if (packet.kind, packet.index) != (kind, index):
                        raise ProtocolError('state_corrupt')
                    if count == 1:
                        if packet.kind != META:
                            raise ProtocolError('state_corrupt')
                        self.check_space(TransferDescriptor.from_bytes(packet.payload).object_size * 2)
                    event = self.receiver.feed(raw)
                    if event.reason not in ('metadata', 'accepted') or self.receiver.state.value == 'error':
                        raise ProtocolError('state_corrupt')
                    self.committed += packet.kind != META
            self.receiver.record = self.record
            self.write_status()
        except BaseException:
            if self.db is not None:
                self.db.close()
            self.lock.close()
            raise

    @property
    def max_state_bytes(self):
        return 3 * self.limits.max_object + self.limits.max_chunks * 8192 + RESERVE

    def check_space(self, required=65536):
        if shutil.disk_usage(self.directory).free < required + RESERVE:
            raise ProtocolError('insufficient_disk')

    def record(self, raw):
        packet = decode(raw)
        if packet.kind == META:
            descriptor = TransferDescriptor.from_bytes(packet.payload, self.limits)
            self.check_space(3 * descriptor.object_size + descriptor.total * 8192)
        else:
            self.check_space()
        with self.db:
            self.db.execute('INSERT INTO packets(kind,idx,raw) VALUES(?,?,?)', (packet.kind, packet.index, raw))
        self.committed += packet.kind != META

    def snapshot(self):
        result = self.receiver.snapshot()
        missing = []
        if self.receiver.descriptor:
            for i in range(self.receiver.descriptor.total):
                if i not in self.receiver.received:
                    if missing and missing[-1][1] == i - 1:
                        missing[-1][1] = i
                    else:
                        missing.append([i, i])
        result.update(storage_schema=1, committed_chunks=self.committed, missing_ranges=missing,
                      state_bytes=sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file()))
        return result

    def write_status(self):
        tmp = self.directory / 'status.tmp'
        with tmp.open('w', encoding='utf-8') as target:
            json.dump(self.snapshot(), target, indent=2)
            target.write('\n'); target.flush(); os.fsync(target.fileno())
        tmp.replace(self.directory / 'status.json')

    def close(self):
        try:
            self.write_status()
        finally:
            self.db.close()
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
