"""Ordered, bounded process pool for independent QR matrix generation."""
from collections import deque
from concurrent.futures import ProcessPoolExecutor
import math
import multiprocessing
import os
from pathlib import Path


def worker_count(requested=0):
    if type(requested) is not int or not 0 <= requested <= 16:
        raise ValueError("workers must be 0..16")
    if requested:
        return requested
    cpus = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        cpus = min(cpus, len(os.sched_getaffinity(0)))
    # Standard cgroup v2 and v1 mount paths; unknown layouts use conservative cap.
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, math.floor(int(quota)/int(period))))
    except (OSError, ValueError):
        try:
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if quota > 0:
                cpus = min(cpus, max(1, quota//period))
        except (OSError, ValueError):
            pass
    return max(1, min(4, cpus))


def encode_frame(raw, generator):
    from .player import matrix, pack_matrix
    return pack_matrix(matrix(raw, generator))


def encoded_frames(sequence, generator="segno", workers=1):
    """At most 2*workers queued frames. Preserve packet order and propagate errors."""
    workers = worker_count(workers)
    if workers == 1:
        for raw in sequence:
            yield encode_frame(raw, generator)
        return
    source = iter(sequence)
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        pending = deque()
        try:
            for _ in range(2*workers):
                raw = next(source, None)
                if raw is None:
                    break
                pending.append(pool.submit(encode_frame, raw, generator))
            while pending:
                yield pending.popleft().result()
                raw = next(source, None)
                if raw is not None:
                    pending.append(pool.submit(encode_frame, raw, generator))
        finally:
            for future in pending:
                future.cancel()
