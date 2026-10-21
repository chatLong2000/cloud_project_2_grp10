"""
Telemetry source: where readings come from.

fetch() returns the readings that became available since the previous call,
with the source clock. FileReplaySource replays a batches.jsonl file (one edge
batch per line); a DynamoDB source will implement the same Protocol on AWS.

INSTANT   the whole file on the first fetch(); the clock is the last sent_at.
REALTIME  timestamps are shifted so that `start_offset_s` into the file maps
          to "now"; each batch is released once the wall clock reaches its
          shifted sent_at. Everything before the offset comes at once.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from analyser.config import ReplayMode
from analyser.models import ParsedBatch, Reading, ValidationError, parse_batch
from analyser.replay_files import iter_batch_lines

log = logging.getLogger("analyser.source")


@dataclass(frozen=True)
class Fetched:
    readings: tuple[Reading, ...]
    now_ms: int  # the source's clock: wall clock live, last batch time in instant replay


class TelemetrySource(Protocol):
    def fetch(self) -> Fetched: ...


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


def load_batches(path: Path | str) -> tuple[tuple[ParsedBatch, ...], int]:
    """Valid batches sorted by sent_at, and the number of skipped lines."""
    batches, rejected = [], 0
    for line_no, line in enumerate(iter_batch_lines(path), start=1):
        try:
            batches.append(parse_batch(line))
        except ValidationError as exc:
            rejected += 1
            log.warning("%s:%d skipped: %s", path, line_no, exc)
    if not batches:
        raise ValueError(f"{path}: no valid batch to replay")
    return tuple(sorted(batches, key=lambda b: b.sent_at)), rejected


class FileReplaySource:
    def __init__(
        self,
        path: Path | str,
        mode: ReplayMode = ReplayMode.INSTANT,
        start_offset_s: float = 0.0,
        wall_clock: Callable[[], int] = wall_clock_ms,
    ):
        self._batches, self.rejected_batches = load_batches(path)
        self._mode = mode
        self._wall_clock = wall_clock
        self._next = 0
        start_ms = self._batches[0].sent_at + int(start_offset_s * 1000)
        self._shift_ms = wall_clock() - start_ms if mode is ReplayMode.REALTIME else 0
        log.info("replaying %d batches from %s (%s, %d skipped)", len(self._batches), path, mode, self.rejected_batches)

    @property
    def exhausted(self) -> bool:
        return self._next >= len(self._batches)

    def fetch(self) -> Fetched:
        now_ms = self._batches[-1].sent_at if self._mode is ReplayMode.INSTANT else self._wall_clock()
        released: list[Reading] = []
        while not self.exhausted and self._batches[self._next].sent_at + self._shift_ms <= now_ms:
            released.extend(self._shifted(self._batches[self._next].readings))
            self._next += 1
        return Fetched(readings=tuple(released), now_ms=now_ms)

    def _shifted(self, readings: tuple[Reading, ...]) -> tuple[Reading, ...]:
        if not self._shift_ms:
            return readings
        return tuple(r.model_copy(update={"timestamp": r.timestamp + self._shift_ms}) for r in readings)
