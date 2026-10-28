"""
Replay file formats.

batches.jsonl     one edge batch per line, as published on coffee/<device_id>/batch:
                  {"edge_id", "device_id", "sent_at" (epoch ms), "readings": [...]}
ground_truth.csv  timestamp (epoch ms), device_id, scenario, event
                  (scenario/event as logged by simulator/scenarios.py)
"""

import csv
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Any

GROUND_TRUTH_HEADER = ("timestamp", "device_id", "scenario", "event")


@dataclass(frozen=True)
class GroundTruthEvent:
    timestamp: int
    device_id: str
    scenario: str
    event: str


def write_batches(path: Path | str, batches: Iterable[Mapping[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for batch in batches:
            f.write(json.dumps(batch) + "\n")
    return path


def iter_batch_lines(path: Path | str) -> Iterator[str]:
    """Yields one raw JSON batch per non-blank line."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"replay file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


def write_ground_truth(path: Path | str, events: Iterable[GroundTruthEvent]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(GROUND_TRUTH_HEADER)
        writer.writerows(astuple(e) for e in events)
    return path


def read_ground_truth(path: Path | str) -> tuple[GroundTruthEvent, ...]:
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = tuple(next(reader, ()))
        if header != GROUND_TRUTH_HEADER:
            raise ValueError(f"unexpected ground truth header {header}, expected {GROUND_TRUTH_HEADER}")
        return tuple(
            GroundTruthEvent(int(ts), device_id, scenario, event) for ts, device_id, scenario, event in reader
        )
