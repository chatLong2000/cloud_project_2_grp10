"""
Offline evaluation of the detectors against the simulator ground truth.

Ground truth events (simulator/scenarios.py) are turned into episodes:
    thermal_runaway   "started"            -> "reboot_triggered"
    memory_leak_fota  first "leak_applied" -> "reboot_triggered_heap_reset"
    out_of_memory     "out_of_memory_triggered" -> "reboot_triggered_heap_reset"
    silent_device     "started ..."        -> "resumed"
An alert matches an episode of the same device and scenario when it opens
between the episode start and its end + tolerance. Recall = detected
episodes / episodes, precision = matching alerts / alerts, latency = first
matching alert - episode start.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from statistics import median
from typing import Any

from analyser.config import ReplayMode, Settings, Thresholds
from analyser.engine import FleetSnapshot, process
from analyser.models import Alert, AlertEvent, Scenario
from analyser.replay_files import GroundTruthEvent
from analyser.source import Fetched, FileReplaySource

DEFAULT_TOLERANCE_MS = 60_000

# (ground truth scenario, event prefix) -> (opens, closes) episode scenarios
_TRANSITIONS: dict[tuple[str, str], tuple[tuple[Scenario, ...], tuple[Scenario, ...]]] = {
    ("thermal_runaway", "started"): ((Scenario.THERMAL_RUNAWAY,), ()),
    ("thermal_runaway", "reboot_triggered"): ((), (Scenario.THERMAL_RUNAWAY,)),
    ("silent_device", "started"): ((Scenario.SILENT_DEVICE,), ()),
    ("silent_device", "resumed"): ((), (Scenario.SILENT_DEVICE,)),
    ("memory_leak_fota", "leak_applied"): ((Scenario.MEMORY_LEAK_FOTA,), ()),
    ("memory_leak_fota", "out_of_memory_triggered"): ((Scenario.OUT_OF_MEMORY,), ()),
    ("memory_leak_fota", "reboot_triggered_heap_reset"): ((), (Scenario.MEMORY_LEAK_FOTA, Scenario.OUT_OF_MEMORY)),
}


@dataclass(frozen=True)
class Episode:
    device_id: str
    scenario: Scenario
    start: int
    end: int
    complete: bool  # False when the data ended before the episode did


@dataclass(frozen=True)
class ScenarioScore:
    scenario: Scenario
    episodes: int
    detected: int
    alerts: int
    false_alerts: int
    recall: float | None
    precision: float | None
    median_latency_s: float | None
    max_latency_s: float | None


@dataclass(frozen=True)
class AnalysisResult:
    snapshot: FleetSnapshot
    alerts: tuple[Alert, ...]


def _transition(event: GroundTruthEvent) -> tuple[tuple[Scenario, ...], tuple[Scenario, ...]]:
    for (scenario, prefix), change in _TRANSITIONS.items():
        if event.scenario == scenario and event.event.startswith(prefix):
            return change
    return (), ()


def episodes_from_ground_truth(events: Iterable[GroundTruthEvent], data_end_ms: int) -> tuple[Episode, ...]:
    episodes: list[Episode] = []
    started: dict[tuple[str, Scenario], int] = {}
    for event in sorted(events, key=lambda e: e.timestamp):
        opens, closes = _transition(event)
        for scenario in closes:
            start = started.pop((event.device_id, scenario), None)
            if start is not None:
                episodes.append(Episode(event.device_id, scenario, start, event.timestamp, True))
        for scenario in opens:
            started.setdefault((event.device_id, scenario), event.timestamp)
    episodes.extend(Episode(dev, sc, start, data_end_ms, False) for (dev, sc), start in started.items())
    return tuple(episodes)


def _matches(alert: Alert, episode: Episode, tolerance_ms: int) -> bool:
    return (
        alert.device_id == episode.device_id
        and alert.scenario is episode.scenario
        and episode.start <= alert.opened_at <= episode.end + tolerance_ms
    )


def _score(scenario: Scenario, alerts: Sequence[Alert], episodes: Sequence[Episode], tol: int) -> ScenarioScore:
    latencies = []
    for episode in episodes:
        hits = [a.opened_at for a in alerts if _matches(a, episode, tol)]
        if hits:
            latencies.append((min(hits) - episode.start) / 1000)
    false_alerts = sum(1 for a in alerts if not any(_matches(a, e, tol) for e in episodes))
    return ScenarioScore(
        scenario=scenario,
        episodes=len(episodes),
        detected=len(latencies),
        alerts=len(alerts),
        false_alerts=false_alerts,
        recall=len(latencies) / len(episodes) if episodes else None,
        precision=(len(alerts) - false_alerts) / len(alerts) if alerts else None,
        median_latency_s=median(latencies) if latencies else None,
        max_latency_s=max(latencies) if latencies else None,
    )


def evaluate(
    alerts: Iterable[Alert], episodes: Iterable[Episode], tolerance_ms: int = DEFAULT_TOLERANCE_MS
) -> tuple[ScenarioScore, ...]:
    alerts, episodes = tuple(alerts), tuple(episodes)
    return tuple(
        _score(
            scenario,
            [a for a in alerts if a.scenario is scenario],
            [e for e in episodes if e.scenario is scenario],
            tolerance_ms,
        )
        for scenario in Scenario
    )


def collect_alerts(events: Iterable[AlertEvent]) -> tuple[Alert, ...]:
    """Latest version of every alert seen in an event stream, in opening order."""
    latest: dict[str, Alert] = {}
    for event in events:
        latest[event.alert.alert_id] = event.alert
    return tuple(latest.values())


def load_replay(batches_path: Path | str) -> Fetched:
    """Every reading of a replay file, validated, with the file's end as clock."""
    return FileReplaySource(batches_path, ReplayMode.INSTANT).fetch()


def analyse(
    fetched: Fetched, thresholds: Thresholds, window_s: float = 600.0, max_alerts: int = 100_000
) -> AnalysisResult:
    """Runs the engine over already loaded readings in one cycle."""
    settings = Settings(replay_path="", window_s=window_s, max_alerts=max_alerts, thresholds=thresholds)
    snapshot, events = process(FleetSnapshot(), fetched, settings)
    return AnalysisResult(snapshot=snapshot, alerts=collect_alerts(events))


def analyse_file(
    batches_path: Path | str, thresholds: Thresholds, window_s: float = 600.0, max_alerts: int = 100_000
) -> AnalysisResult:
    return analyse(load_replay(batches_path), thresholds, window_s, max_alerts)


def sweep(
    fetched: Fetched,
    episodes: Sequence[Episode],
    base: Thresholds,
    parameter: str,
    values: Iterable[float],
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
) -> list[tuple[float, tuple[ScenarioScore, ...]]]:
    """Scores for each value of one threshold, the others staying at `base`."""
    if parameter not in {f.name for f in fields(Thresholds)}:
        raise ValueError(f"unknown threshold {parameter!r}")
    results = []
    for value in values:
        changes: dict[str, Any] = {parameter: value}
        result = analyse(fetched, replace(base, **changes))
        results.append((value, evaluate(result.alerts, episodes, tolerance_ms)))
    return results
