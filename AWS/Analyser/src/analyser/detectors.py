"""
Scenario detectors (doc/G10-architecture-V2.md §4).

Each detector is a pure step function over one device's readings, in
timestamp order:
    state, detection = step(state, reading, thresholds)
`detection.level` is the scenario's current severity (None = not present).
Turning levels into alerts (open / escalate / resolve) is alerts.track()'s job.

thermal_runaway   temperature > setpoint + delta on N consecutive readings
                  (WARNING), overheat bit or critical temperature (CRITICAL).
memory_leak_fota  median standby heap before vs after each firmware update; a
                  drop on `leak_streak` consecutive updates (WARNING). Only
                  standby samples count, so brewing allocations do not.
out_of_memory     out_of_memory bit or heap at/below the critical level (CRITICAL).
silent_device     gap between two readings > silent_gap_s (WARNING), seen when
                  data resumes, from measurement timestamps.
device_offline    no reading for > offline_after_s at the source clock
                  (CRITICAL); checked every cycle by the engine, not per reading.
Transient faults (capsule_jam, no_water, no_capsule) are shown, never alerted.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from statistics import median
from typing import Any, NamedTuple

from analyser.config import Thresholds
from analyser.models import TRANSIENT_FAULTS, DeviceStatus, ErrorBit, Reading, Scenario, Severity

REFERENCE_SAMPLES_FACTOR = 3  # leak reference: median of the last 3 x leak_min_samples standby samples


@dataclass(frozen=True)
class Detection:
    level: Severity | None = None
    message: str = ""


CLEAR = Detection()

Step = Callable[[Any, Reading, Thresholds], tuple[Any, Detection]]


class Detector(NamedTuple):
    scenario: Scenario
    step: Step
    initial: Any  # immutable initial state, shared by every device


def thermal_step(hot_streak: int, reading: Reading, th: Thresholds) -> tuple[int, Detection]:
    """State: number of consecutive readings above the warning temperature."""
    temp = reading.temperature_c
    hot_streak = hot_streak + 1 if temp > th.thermal_warn_c else 0
    if ErrorBit.OVERHEAT in reading.error_bits or temp >= th.thermal_critical_c:
        return hot_streak, Detection(Severity.CRITICAL, f"overheat: {temp:.1f} °C")
    if hot_streak >= th.thermal_consecutive:
        return hot_streak, Detection(
            Severity.WARNING, f"{temp:.1f} °C > {th.thermal_warn_c:.1f} °C for {hot_streak} readings"
        )
    return hot_streak, CLEAR


@dataclass(frozen=True)
class LeakState:
    in_update: bool = False
    standby_heaps: tuple[int, ...] = ()  # standby samples since the last update, bounded
    reference: float | None = None  # standby median before the last update
    awaiting_comparison: bool = False
    streak: int = 0  # consecutive updates followed by a heap drop


def leak_step(state: LeakState, reading: Reading, th: Thresholds) -> tuple[LeakState, Detection]:
    if ErrorBit.OUT_OF_MEMORY in reading.error_bits:
        return LeakState(), CLEAR  # crash: the heap is reset, out_of_memory takes over
    if reading.device_status is DeviceStatus.UPDATING:
        state = state if state.in_update else _enter_update(state, th)
    elif reading.device_status is DeviceStatus.STANDBY:
        state = _add_standby_sample(replace(state, in_update=False), reading.heap_free_bytes, th)
    else:
        state = replace(state, in_update=False)

    if state.streak < th.leak_streak:
        return state, CLEAR
    current = median(state.standby_heaps) if state.standby_heaps else 0
    message = f"standby heap dropped after {state.streak} consecutive updates (now ~{current:.0f} B)"
    return state, Detection(Severity.WARNING, message)


def _enter_update(state: LeakState, th: Thresholds) -> LeakState:
    """Freezes the "before update" reference, if there are enough standby samples."""
    enough = len(state.standby_heaps) >= th.leak_min_samples
    return replace(
        state,
        in_update=True,
        standby_heaps=(),
        reference=median(state.standby_heaps) if enough else None,
        awaiting_comparison=enough,
    )


def _add_standby_sample(state: LeakState, heap: int, th: Thresholds) -> LeakState:
    """Adds a sample; once enough "after update" samples exist, compares them to the reference once."""
    keep = th.leak_min_samples * REFERENCE_SAMPLES_FACTOR
    state = replace(state, standby_heaps=(*state.standby_heaps, heap)[-keep:])
    if state.reference is None or not state.awaiting_comparison or len(state.standby_heaps) < th.leak_min_samples:
        return state
    dropped = state.reference - median(state.standby_heaps) >= th.leak_min_drop_bytes
    return replace(state, awaiting_comparison=False, streak=state.streak + 1 if dropped else 0)


def oom_step(state: None, reading: Reading, th: Thresholds) -> tuple[None, Detection]:
    heap = reading.heap_free_bytes
    if ErrorBit.OUT_OF_MEMORY in reading.error_bits or heap <= th.oom_critical_free_bytes:
        return None, Detection(Severity.CRITICAL, f"out of memory: {heap} B free")
    return None, CLEAR


def silent_step(last_ts: int | None, reading: Reading, th: Thresholds) -> tuple[int, Detection]:
    """State: timestamp of the previous reading."""
    if last_ts is not None:
        gap_s = (reading.timestamp - last_ts) / 1000
        if gap_s > th.silent_gap_s:
            return reading.timestamp, Detection(Severity.WARNING, f"no data for {gap_s:.0f} s")
    return reading.timestamp, CLEAR


def offline_detection(last_ts: int, now_ms: int, th: Thresholds) -> Detection:
    silent_s = (now_ms - last_ts) / 1000
    if silent_s > th.offline_after_s:
        return Detection(Severity.CRITICAL, f"offline: no data for {silent_s:.0f} s")
    return CLEAR


def active_faults(reading: Reading) -> tuple[str, ...]:
    bits = reading.error_bits & TRANSIENT_FAULTS
    return tuple(str(bit.name).lower() for bit in ErrorBit if bit in bits)


# Order matters: it is the order in which alert events are emitted for a reading.
DETECTORS: tuple[Detector, ...] = (
    Detector(Scenario.THERMAL_RUNAWAY, thermal_step, 0),
    Detector(Scenario.MEMORY_LEAK_FOTA, leak_step, LeakState()),
    Detector(Scenario.OUT_OF_MEMORY, oom_step, None),
    Detector(Scenario.SILENT_DEVICE, silent_step, None),
)
