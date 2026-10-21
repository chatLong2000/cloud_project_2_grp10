"""
Alert lifecycle and delivery.

track() turns a detector level into alert events without duplicates: an
alert opens when a condition appears, escalates when it gets more severe
(never downgrades) and resolves when it clears. At most one open alert per
(device, scenario). The AlertBook is immutable: track() returns a new one.

Sinks deliver events: logs and a JSONL file locally, SNS on AWS later. A
failing sink is logged and never stops the analyser or the other sinks.
"""

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from analyser.detectors import Detection
from analyser.models import Alert, AlertEvent, AlertEventKind, AlertState, Scenario

log = logging.getLogger("analyser.alerts")

AlertKey = tuple[str, Scenario]


@dataclass(frozen=True)
class AlertBook:
    open: Mapping[AlertKey, Alert] = field(default_factory=dict)
    history: tuple[Alert, ...] = ()  # latest version of each alert, least recently updated first, bounded


def _record(book: AlertBook, alert: Alert, max_alerts: int) -> AlertBook:
    """Book with `alert` recorded; beyond `max_alerts`, the oldest resolved alerts go, open ones stay."""
    key = (alert.device_id, alert.scenario)
    if alert.state is AlertState.OPEN:
        open_alerts = {**book.open, key: alert}
    else:
        open_alerts = {k: v for k, v in book.open.items() if k != key}

    history = (*(a for a in book.history if a.alert_id != alert.alert_id), alert)
    excess = len(history) - max_alerts
    if excess > 0:
        evicted = {a.alert_id for a in [a for a in history if a.state is AlertState.RESOLVED][:excess]}
        history = tuple(a for a in history if a.alert_id not in evicted)
    return AlertBook(open=open_alerts, history=history)


def track(
    book: AlertBook, device_id: str, scenario: Scenario, detection: Detection, at_ms: int, max_alerts: int
) -> tuple[AlertBook, AlertEvent | None]:
    current = book.open.get((device_id, scenario))
    level = detection.level

    if current is None and level is None:
        return book, None
    if current is None:
        alert = Alert(
            alert_id=f"{device_id}:{scenario}:{at_ms}",
            device_id=device_id,
            scenario=scenario,
            severity=level,
            state=AlertState.OPEN,
            opened_at=at_ms,
            updated_at=at_ms,
            message=detection.message,
        )
        kind = AlertEventKind.OPENED
    elif level is None:
        alert = current.model_copy(update={"state": AlertState.RESOLVED, "resolved_at": at_ms, "updated_at": at_ms})
        kind = AlertEventKind.RESOLVED
    elif level.rank > current.severity.rank:
        alert = current.model_copy(update={"severity": level, "message": detection.message, "updated_at": at_ms})
        kind = AlertEventKind.ESCALATED
    else:
        return book, None
    return _record(book, alert, max_alerts), AlertEvent(kind=kind, alert=alert)


class AlertSink(Protocol):
    def publish(self, event: AlertEvent) -> None: ...


class LogAlertSink:
    def publish(self, event: AlertEvent) -> None:
        a = event.alert
        level = logging.INFO if event.kind is AlertEventKind.RESOLVED else logging.WARNING
        log.log(level, "alert %s %s %s on %s: %s", event.kind, a.severity, a.scenario, a.device_id, a.message)


class JsonlAlertSink:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def publish(self, event: AlertEvent) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.model_dump(mode="json")) + "\n")


def publish_all(sinks: Iterable[AlertSink], events: Iterable[AlertEvent]) -> None:
    events = tuple(events)
    for sink in sinks:
        for event in events:
            try:
                sink.publish(event)
            except Exception:
                log.exception("alert sink %s failed for %s", type(sink).__name__, event.alert.alert_id)
