"""
Domain types and validation of incoming edge batches.

A Reading mirrors one CSV line of the simulator, as forwarded by the edge.
A batch is the JSON the edge publishes on coffee/<device_id>/batch:
    {"edge_id": str, "device_id": str, "sent_at": epoch_ms, "readings": [...]}
A malformed envelope raises ValidationError; an invalid reading inside a valid
envelope is dropped, counted and logged. All models are immutable.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntFlag, StrEnum
from typing import Any

import pydantic
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("analyser.models")

DEVICE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"  # also enforced on the API query parameter


class DeviceStatus(StrEnum):
    OFF = "off"
    BOOTING = "booting"
    PREHEAT = "preheat"
    STANDBY = "standby"
    BREWING = "brewing"
    UPDATING = "updating"
    DEGRADED = "degraded"
    ERROR = "error"


class ErrorBit(IntFlag):
    """error_code bitfield, see simulator/scenarios.py."""

    OVERHEAT = 1
    OUT_OF_MEMORY = 2
    CAPSULE_JAM = 4
    NO_WATER = 8
    NO_CAPSULE = 16


TRANSIENT_FAULTS = ErrorBit.CAPSULE_JAM | ErrorBit.NO_WATER | ErrorBit.NO_CAPSULE


class Scenario(StrEnum):
    THERMAL_RUNAWAY = "thermal_runaway"
    MEMORY_LEAK_FOTA = "memory_leak_fota"
    OUT_OF_MEMORY = "out_of_memory"
    SILENT_DEVICE = "silent_device"
    DEVICE_OFFLINE = "device_offline"


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return 2 if self is Severity.CRITICAL else 1


class AlertState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class AlertEventKind(StrEnum):
    OPENED = "opened"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Reading(BaseModel):
    model_config = _FROZEN

    timestamp: int = Field(ge=0, description="Epoch ms")
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    uptime_ms: int = Field(ge=0)
    temperature_c: float = Field(ge=-40.0, le=200.0, allow_inf_nan=False)
    power_consumption_mw: float = Field(ge=0.0, allow_inf_nan=False)
    heap_free_bytes: int = Field(ge=0)
    device_status: DeviceStatus
    error_code: int = Field(ge=0)

    @property
    def error_bits(self) -> ErrorBit:
        return ErrorBit(self.error_code)


class Alert(BaseModel):
    model_config = _FROZEN

    alert_id: str
    device_id: str
    scenario: Scenario
    severity: Severity
    state: AlertState
    opened_at: int
    updated_at: int
    resolved_at: int | None = None
    message: str = ""


class AlertEvent(BaseModel):
    model_config = _FROZEN

    kind: AlertEventKind
    alert: Alert


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedBatch:
    device_id: str
    sent_at: int
    readings: tuple[Reading, ...]
    rejected: int


def parse_reading(raw: Any) -> Reading:
    """Returns a typed Reading, or raises ValidationError naming the bad fields."""
    if not isinstance(raw, Mapping):
        raise ValidationError(f"reading must be an object, got {type(raw).__name__}")
    try:
        return Reading.model_validate(dict(raw), strict=False)
    except pydantic.ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in err["loc"]) or "?" for err in exc.errors())
        raise ValidationError(f"invalid reading ({fields})") from None


def parse_batch(raw: Mapping[str, Any] | str) -> ParsedBatch:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"batch is not valid JSON: {exc.msg}") from None
    if not isinstance(raw, Mapping):
        raise ValidationError("batch must be a JSON object")

    device_id, readings, sent_at = raw.get("device_id"), raw.get("readings"), raw.get("sent_at")
    if not isinstance(device_id, str) or not device_id:
        raise ValidationError("batch has no device_id")
    if not isinstance(readings, list):
        raise ValidationError(f"batch {device_id}: readings must be a list")
    if isinstance(sent_at, bool) or not isinstance(sent_at, int) or sent_at < 0:
        raise ValidationError(f"batch {device_id}: sent_at must be a positive epoch ms")

    valid: list[Reading] = []
    for item in readings:
        try:
            reading = parse_reading(item)
        except ValidationError as exc:
            log.warning("batch %s: rejected reading: %s", device_id, exc)
            continue
        if reading.device_id != device_id:
            log.warning("batch %s: rejected reading from %s", device_id, reading.device_id)
            continue
        valid.append(reading)
    return ParsedBatch(device_id, sent_at, tuple(valid), rejected=len(readings) - len(valid))
