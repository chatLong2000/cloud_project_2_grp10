"""
Analysis engine: one pure step per analyser cycle.

    snapshot', events = process(snapshot, fetched, settings)

For each device, readings strictly newer than the last one processed go
through every detector in timestamp order, detections become alert events,
and the device's sliding window is extended. Then every known device is
checked for being offline at the source clock. The previous snapshot is
never modified, so the API keeps serving it while the next one is computed.
"""

import logging
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from analyser.alerts import AlertBook, track
from analyser.config import Settings
from analyser.detectors import DETECTORS, Detector, active_faults, offline_detection
from analyser.models import AlertEvent, AlertState, Reading, Scenario
from analyser.source import Fetched

log = logging.getLogger("analyser.engine")

# A reading this far ahead of the source clock would make every later reading look late
# (clock fault on a device): it is dropped instead.
MAX_FUTURE_SKEW_MS = 5 * 60 * 1000


def _timestamp(reading: Reading) -> int:
    return reading.timestamp


@dataclass(frozen=True)
class DeviceTrack:
    readings: tuple[Reading, ...] = ()  # sliding window, oldest first
    states: tuple[Any, ...] = ()  # one state per detector
    last_seen: int | None = None

    @property
    def last(self) -> Reading | None:
        return self.readings[-1] if self.readings else None

    def between(self, from_ms: int, to_ms: int) -> tuple[Reading, ...]:
        lo = bisect_left(self.readings, from_ms, key=_timestamp)
        hi = bisect_right(self.readings, to_ms, key=_timestamp)
        return self.readings[lo:hi]


@dataclass(frozen=True)
class FleetSnapshot:
    devices: Mapping[str, DeviceTrack] = field(default_factory=dict)
    alerts: AlertBook = AlertBook()
    now_ms: int = 0
    late_readings: int = 0  # older than the last processed reading of their device
    future_readings: int = 0  # ahead of the source clock by more than MAX_FUTURE_SKEW_MS
    device_errors: int = 0  # device batches skipped because their analysis failed

    def is_online(self, device_id: str) -> bool:
        return (device_id, Scenario.DEVICE_OFFLINE) not in self.alerts.open

    def device_views(self) -> list[dict[str, Any]]:
        return [self._device_view(device_id) for device_id in sorted(self.devices)]

    def _device_view(self, device_id: str) -> dict[str, Any]:
        device = self.devices[device_id]
        last = device.last
        open_alerts = sorted(
            (a for (dev, _), a in self.alerts.open.items() if dev == device_id), key=lambda a: -a.severity.rank
        )
        return {
            "device_id": device_id,
            "online": self.is_online(device_id),
            "last_seen": device.last_seen,
            "device_status": last.device_status.value if last else None,
            "temperature_c": last.temperature_c if last else None,
            "heap_free_bytes": last.heap_free_bytes if last else None,
            "power_consumption_mw": last.power_consumption_mw if last else None,
            "error_code": last.error_code if last else None,
            "active_faults": list(active_faults(last)) if last else [],
            "open_alerts": [a.scenario.value for a in open_alerts],
            "severity": open_alerts[0].severity.value if open_alerts else None,
        }

    def telemetry(self, device_id: str | None, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        ids = [device_id] if device_id else sorted(self.devices)
        rows = [
            r.model_dump(mode="json")
            for dev in ids
            if dev in self.devices
            for r in self.devices[dev].between(from_ms, to_ms)
        ]
        return sorted(rows, key=lambda row: row["timestamp"])

    def alerts_between(self, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        """Alerts active at some point in [from_ms, to_ms], newest first."""
        selected = [
            a
            for a in self.alerts.history
            if a.opened_at <= to_ms and (a.state is AlertState.OPEN or (a.resolved_at or 0) >= from_ms)
        ]
        return [a.model_dump(mode="json") for a in sorted(selected, key=lambda a: -a.opened_at)]


def process(
    snapshot: FleetSnapshot, fetched: Fetched, settings: Settings, detectors: Sequence[Detector] = DETECTORS
) -> tuple[FleetSnapshot, tuple[AlertEvent, ...]]:
    by_device, future = _group_by_device(fetched)
    devices = dict(snapshot.devices)
    book, events = snapshot.alerts, []
    late, errors = snapshot.late_readings, snapshot.device_errors

    for device_id in sorted(by_device):
        previous = devices.get(device_id)
        if previous is None:
            previous = DeviceTrack(states=tuple(d.initial for d in detectors))
        try:
            device, book, device_events, dropped = _process_device(
                device_id, previous, by_device[device_id], book, settings, detectors
            )
        except Exception:  # one bad device must not stop the analysis of the fleet
            log.exception("%s: analysis failed, %d readings skipped", device_id, len(by_device[device_id]))
            errors += 1
            continue
        devices[device_id] = device
        events.extend(device_events)
        late += dropped

    for device_id, device in sorted(devices.items()):
        if device.last_seen is None:
            continue
        detection = offline_detection(device.last_seen, fetched.now_ms, settings.thresholds)
        book, event = track(book, device_id, Scenario.DEVICE_OFFLINE, detection, fetched.now_ms, settings.max_alerts)
        if event:
            events.append(event)

    new_snapshot = FleetSnapshot(devices, book, fetched.now_ms, late, snapshot.future_readings + future, errors)
    return new_snapshot, tuple(events)


def _group_by_device(fetched: Fetched) -> tuple[dict[str, list[Reading]], int]:
    by_device: dict[str, list[Reading]] = {}
    future = 0
    for reading in fetched.readings:
        if reading.timestamp > fetched.now_ms + MAX_FUTURE_SKEW_MS:
            future += 1
            continue
        by_device.setdefault(reading.device_id, []).append(reading)
    if future:
        log.warning("dropped %d readings timestamped in the future (clock fault?)", future)
    return by_device, future


def _process_device(
    device_id: str,
    device: DeviceTrack,
    readings: Sequence[Reading],
    book: AlertBook,
    settings: Settings,
    detectors: Sequence[Detector],
) -> tuple[DeviceTrack, AlertBook, list[AlertEvent], int]:
    """New device track and alert book, the device's events, and the number of late/duplicate readings."""
    accepted = _new_readings(device.last_seen, readings)
    dropped = len(readings) - len(accepted)
    if dropped:
        log.debug("%s: ignored %d late or duplicate readings", device_id, dropped)
    if not accepted:
        return device, book, [], dropped

    states, events = list(device.states), []
    for reading in accepted:
        for i, detector in enumerate(detectors):
            states[i], detection = detector.step(states[i], reading, settings.thresholds)
            book, event = track(book, device_id, detector.scenario, detection, reading.timestamp, settings.max_alerts)
            if event:
                events.append(event)

    # accepted readings are all newer than the window: append, then drop what is older than window_s
    window = (*device.readings, *accepted)
    cutoff = window[-1].timestamp - int(settings.window_s * 1000)
    window = window[bisect_right(window, cutoff, key=_timestamp) :]
    return DeviceTrack(window, tuple(states), accepted[-1].timestamp), book, events, dropped


def _new_readings(last_seen: int | None, readings: Sequence[Reading]) -> list[Reading]:
    """Readings strictly newer than `last_seen` and than each other, in timestamp order."""
    accepted: list[Reading] = []
    for reading in sorted(readings, key=_timestamp):
        newest = accepted[-1].timestamp if accepted else last_seen
        if newest is None or reading.timestamp > newest:
            accepted.append(reading)
    return accepted
