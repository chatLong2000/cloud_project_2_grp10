"""
Replay dataset generator.

Drives the real simulator (simulator/device_simulator.py, unmodified) for a
fleet of devices on a simulated clock, so hours of data are produced in
seconds. Readings are grouped into edge-shaped batches; every fault event
the simulator logs is kept as ground truth, attributed to its device.

    uv run python -m analyser.generator --devices 10 --hours 8 --seed 42 --out data/replay/ref

The simulator only reads the wall clock in device_simulator._epoch_millis()
and in its ground-truth logger: the first is swapped for the simulated clock
during generation (and restored afterwards), the second is replaced by a
recorder passed to DeviceSimulator.
"""

import argparse
import copy
import importlib
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyser.replay_files import GroundTruthEvent, write_batches, write_ground_truth

log = logging.getLogger("analyser.generator")

DEFAULT_SIMULATOR_DIR = Path(__file__).resolve().parents[4] / "simulator"
DEFAULT_START_MS = int(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
DEFAULT_BATCH_INTERVAL_S = 15
EDGE_ID = "replay"


@dataclass(frozen=True)
class Replay:
    batches: tuple[dict[str, Any], ...]
    ground_truth: tuple[GroundTruthEvent, ...]


class _SimClock:
    def __init__(self, now_ms: int):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class _GroundTruthRecorder:
    """Duck-typed stand-in for simulator/ground_truth.GroundTruthLogger."""

    def __init__(self, device_id: str, clock: _SimClock, sink: list[GroundTruthEvent]):
        self._device_id = device_id
        self._clock = clock
        self._sink = sink

    def log(self, scenario: str, event: str) -> None:
        self._sink.append(GroundTruthEvent(self._clock(), self._device_id, scenario, event))

    def close(self) -> None:
        pass


def device_id_for(index: int) -> str:
    return f"coffee-machine-{index:02d}"


def _load_simulator(simulator_dir: Path) -> Any:
    if not (simulator_dir / "device_simulator.py").is_file():
        raise FileNotFoundError(f"simulator not found in {simulator_dir} (expected device_simulator.py)")
    if str(simulator_dir) not in sys.path:
        sys.path.insert(0, str(simulator_dir))
    return importlib.import_module("device_simulator")


def _device_config(base_cfg: dict, index: int, seed: int, silent: bool) -> dict:
    cfg = copy.deepcopy(base_cfg)
    cfg["device_id"] = device_id_for(index)
    cfg["random_seed"] = seed + index
    cfg["silent_device"]["enabled"] = silent
    return cfg


def _to_batches(buffers: dict[str, tuple[dict, ...]], sent_at: int) -> list[dict[str, Any]]:
    return [
        {"edge_id": EDGE_ID, "device_id": device_id, "sent_at": sent_at, "readings": list(rows)}
        for device_id, rows in sorted(buffers.items())
        if rows
    ]


def generate(
    devices: int = 10,
    hours: float = 8.0,
    seed: int = 42,
    start_ms: int = DEFAULT_START_MS,
    batch_interval_s: int = DEFAULT_BATCH_INTERVAL_S,
    silent: bool = True,
    simulator_dir: Path | str = DEFAULT_SIMULATOR_DIR,
) -> Replay:
    if devices < 1:
        raise ValueError(f"devices must be >= 1, got {devices}")
    if hours <= 0:
        raise ValueError(f"hours must be > 0, got {hours}")

    sim_module = _load_simulator(Path(simulator_dir))
    base_cfg = sim_module.load_config(Path(simulator_dir) / "config.yaml")
    interval_s = base_cfg["sample_interval_seconds"]
    tick_ms = int(interval_s * 1000)
    batch_ms = batch_interval_s * 1000

    clock = _SimClock(start_ms)
    ground_truth: list[GroundTruthEvent] = []
    simulators = []
    for i in range(1, devices + 1):
        cfg = _device_config(base_cfg, i, seed, silent)
        recorder = _GroundTruthRecorder(cfg["device_id"], clock, ground_truth)
        simulators.append(sim_module.DeviceSimulator(cfg, recorder))

    original_clock: Callable[[], int] = sim_module._epoch_millis
    sim_module._epoch_millis = clock
    try:
        batches = _run(simulators, clock, start_ms, int(hours * 3600 * 1000 / tick_ms), tick_ms, batch_ms, interval_s)
    finally:
        sim_module._epoch_millis = original_clock

    log.info("generated %d batches and %d ground truth events", len(batches), len(ground_truth))
    return Replay(batches=tuple(batches), ground_truth=tuple(ground_truth))


def _run(simulators, clock, start_ms, ticks, tick_ms, batch_ms, interval_s) -> list[dict[str, Any]]:
    empty: dict[str, tuple[dict, ...]] = {sim.cfg["device_id"]: () for sim in simulators}
    buffers = empty
    batches: list[dict[str, Any]] = []
    last_flush = start_ms
    for tick in range(1, ticks + 1):
        clock.now_ms = start_ms + tick * tick_ms
        rows = [row for row in (sim.tick(interval_s) for sim in simulators) if row is not None]
        buffers = {**buffers, **{row["device_id"]: (*buffers[row["device_id"]], row) for row in rows}}
        if clock.now_ms - last_flush >= batch_ms:
            batches.extend(_to_batches(buffers, clock.now_ms))
            buffers = empty
            last_flush = clock.now_ms
    batches.extend(_to_batches(buffers, clock.now_ms))
    return batches


def write_replay(replay: Replay, out_dir: Path | str) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    return (
        write_batches(out_dir / "batches.jsonl", replay.batches),
        write_ground_truth(out_dir / "ground_truth.csv", replay.ground_truth),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate a replay dataset from the coffee machine simulator")
    parser.add_argument("--devices", type=int, default=10)
    parser.add_argument("--hours", type=float, default=8.0, help="simulated duration")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-interval-s", type=int, default=DEFAULT_BATCH_INTERVAL_S)
    parser.add_argument("--no-silent", action="store_true", help="disable the silent_device scenario")
    parser.add_argument("--simulator-dir", default=str(DEFAULT_SIMULATOR_DIR))
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    replay = generate(
        devices=args.devices,
        hours=args.hours,
        seed=args.seed,
        batch_interval_s=args.batch_interval_s,
        silent=not args.no_silent,
        simulator_dir=args.simulator_dir,
    )
    batches_path, gt_path = write_replay(replay, args.out)
    log.info("wrote %s and %s", batches_path, gt_path)


if __name__ == "__main__":
    main()
