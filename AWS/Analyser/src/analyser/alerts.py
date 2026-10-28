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
from typing import Any, Protocol

from analyser.detectors import Detection
from analyser.models import Alert, AlertEvent, AlertEventKind, AlertState, Scenario, Severity

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


class SnsAlertSink:
    """
    Publishes alert events to an Amazon SNS topic.

    Attaches MessageAttributes for server-side subscription filtering
    (e.g. by severity, scenario, or event kind) and trims subjects to 100 chars.
    """

    def __init__(
        self,
        topic_arn: str,
        region: str = "us-east-1",
        client: Any = None,
        notify_resolved: bool = True,
        min_severity: Severity = Severity.WARNING,
    ):
        self._topic_arn = topic_arn
        self._region = region
        if client is None:
            import boto3

            self._client = boto3.client("sns", region_name=region)
        else:
            self._client = client
        self._notify_resolved = notify_resolved
        self._min_severity = min_severity

    def publish(self, event: AlertEvent) -> None:
        a = event.alert
        if event.kind is AlertEventKind.RESOLVED and not self._notify_resolved:
            return
        if event.kind is not AlertEventKind.RESOLVED and a.severity.rank < self._min_severity.rank:
            return

        prefix = f"[SDATAWAY - {event.kind.value.upper()}]"
        subject = f"{prefix} {a.severity.value.upper()}: {a.scenario.value} on {a.device_id}"
        if len(subject) > 100:
            subject = subject[:97] + "..."

        body = (
            f"=== SDATAWAY ALERT EVENT: {event.kind.value.upper()} ===\n\n"
            f"Device ID   : {a.device_id}\n"
            f"Scenario    : {a.scenario.value}\n"
            f"Severity    : {a.severity.value.upper()}\n"
            f"State       : {a.state.value}\n"
            f"Message     : {a.message}\n"
            f"Alert ID    : {a.alert_id}\n\n"
            f"Event Payload:\n{json.dumps(event.model_dump(mode='json'), indent=2)}\n"
        )

        attributes = {
            "severity": {"DataType": "String", "StringValue": a.severity.value},
            "scenario": {"DataType": "String", "StringValue": a.scenario.value},
            "device_id": {"DataType": "String", "StringValue": a.device_id},
            "event_kind": {"DataType": "String", "StringValue": event.kind.value},
        }

        self._client.publish(
            TopicArn=self._topic_arn,
            Subject=subject,
            Message=body,
            MessageAttributes=attributes,
        )


class TimestreamAlertSink:
    """
    Persists alert events to the Amazon Timestream 'alerts' table using Multi-Measure records.
    """

    def __init__(
        self,
        database: str,
        table: str,
        region: str = "us-east-1",
        client: Any = None,
    ):
        self._database = database
        self._table = table
        self._region = region
        if client is None:
            import boto3

            self._client = boto3.client("timestream-write", region_name=region)
        else:
            self._client = client

    def publish(self, event: AlertEvent) -> None:
        a = event.alert
        record = {
            "Dimensions": [
                {"Name": "device_id", "Value": str(a.device_id)},
                {"Name": "scenario", "Value": str(a.scenario.value)},
                {"Name": "severity", "Value": str(a.severity.value)},
                {"Name": "state", "Value": str(a.state.value)},
                {"Name": "event_kind", "Value": str(event.kind.value)},
            ],
            "MeasureName": "alert_event",
            "MeasureValueType": "MULTI",
            "Time": str(a.updated_at),
            "TimeUnit": "MILLISECONDS",
            "MeasureValues": [
                {"Name": "alert_id", "Value": str(a.alert_id), "Type": "VARCHAR"},
                {"Name": "message", "Value": str(a.message or ""), "Type": "VARCHAR"},
                {"Name": "severity_rank", "Value": str(a.severity.rank), "Type": "BIGINT"},
                {"Name": "opened_at", "Value": str(a.opened_at), "Type": "BIGINT"},
                {"Name": "resolved_at", "Value": str(a.resolved_at or 0), "Type": "BIGINT"},
            ],
        }
        try:
            self._client.write_records(
                DatabaseName=self._database,
                TableName=self._table,
                Records=[record],
            )
        except Exception as exc:
            reasons = getattr(exc, "response", {}).get("RejectedRecords", [])
            if reasons:
                for r in reasons:
                    log.error("timestream alert record rejected: %s", r.get("Reason"))
            raise


def publish_all(sinks: Iterable[AlertSink], events: Iterable[AlertEvent]) -> None:
    events = tuple(events)
    for sink in sinks:
        for event in events:
            try:
                sink.publish(event)
            except Exception:
                log.exception("alert sink %s failed for %s", type(sink).__name__, event.alert.alert_id)
