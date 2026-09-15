"""Deterministic open-loop schedule: never uses receiver feedback."""
from __future__ import annotations

import random


def schedule(count: int, *, seed: int = 1, loss: float = 0.1, duplicate: float = 0.1,
             corrupt: float = 0.05, late: int = 0, burst_start: int = 0,
             burst_length: int = 0, reorder: bool = True) -> dict:
    if any(not 0 <= value <= 1 for value in (loss, duplicate, corrupt)) or min(count, late, burst_start, burst_length) < 0:
        raise ValueError("Invalid simulation parameters")
    rng = random.Random(seed)
    records = []
    for index in range(count):
        drop = index < late or burst_start <= index < burst_start + burst_length or rng.random() < loss
        mutation = rng.choice(("header", "payload")) if rng.random() < corrupt else "none"
        records.append({"source": index, "drop": drop, "mutation": mutation,
                        "copies": 2 if rng.random() < duplicate else 1})
    order = [i for i, record in enumerate(records) if not record["drop"]]
    if reorder:
        rng.shuffle(order)
    return {"schema": 1, "seed": seed, "records": records, "order": order}


def replay(sequence: list[bytes], plan: dict):
    if plan.get("schema") != 1 or len(plan["records"]) != len(sequence):
        raise ValueError("Schedule does not match source")
    for position in plan["order"]:
        record = plan["records"][position]
        if record["drop"]:
            continue
        raw = bytearray(sequence[record["source"]])
        if record["mutation"] == "header":
            raw[12] ^= 1  # Transfer ID; CRC must catch it.
        elif record["mutation"] == "payload":
            raw[-1] ^= 1
        elif record["mutation"] != "none":
            raise ValueError("Unknown mutation")
        if record["copies"] not in (1, 2):
            raise ValueError("Invalid copy count")
        for _ in range(record["copies"]):
            yield bytes(raw)
