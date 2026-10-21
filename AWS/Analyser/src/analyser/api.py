"""
Analyser service and the JSON API read by Grafana (Infinity data source, doc/adr/0001).

AnalyserService owns the current snapshot and runs analysis cycles. The
snapshot is replaced, never modified, so a request always reads a consistent
state; a failing cycle is logged and reported by /health while the previous
snapshot keeps being served.

    GET /health          last cycle status (503 until the first cycle or after a failed one)
    GET /api/devices     current state of every device
    GET /api/telemetry   readings, ?device_id=&from=&to= (epoch ms, default: last 5 min)
    GET /api/alerts      alerts active in [from, to], ?state=open|resolved|all

Every response uses the envelope {success, data, error, meta}. The API is meant
for the internal Docker network, Grafana being the only published service.
"""

import asyncio
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from analyser.alerts import AlertSink, publish_all
from analyser.config import Settings
from analyser.engine import FleetSnapshot, process
from analyser.models import DEVICE_ID_PATTERN, AlertEvent
from analyser.source import TelemetrySource

log = logging.getLogger("analyser.api")

DEFAULT_RANGE_MS = 5 * 60 * 1000


class AnalyserService:
    def __init__(self, source: TelemetrySource, settings: Settings, sinks: Iterable[AlertSink]):
        self.source = source
        self.settings = settings
        self.sinks = tuple(sinks)
        self._snapshot = FleetSnapshot()
        self.attempts = 0
        self.cycles = 0
        self.last_cycle_at: int | None = None
        self.last_error: str | None = None

    @property
    def snapshot(self) -> FleetSnapshot:
        return self._snapshot

    def run_cycle(self) -> tuple[AlertEvent, ...]:
        self.attempts += 1
        fetched = self.source.fetch()
        self._snapshot, events = process(self._snapshot, fetched, self.settings)
        publish_all(self.sinks, events)
        self.cycles += 1
        self.last_cycle_at = fetched.now_ms
        self.last_error = None
        log.debug("cycle %d: %d readings, %d alert events", self.cycles, len(fetched.readings), len(events))
        return events

    def health(self) -> dict[str, Any]:
        if self.last_error is not None:
            status = "error"
        elif self.cycles == 0:
            status = "starting"
        else:
            status = "ok"
        return {
            "status": status,
            "cycles": self.cycles,
            "last_cycle_at": self.last_cycle_at,
            "last_error": self.last_error,
            "devices": len(self._snapshot.devices),
            "late_readings": self._snapshot.late_readings,
            "future_readings": self._snapshot.future_readings,
            "device_errors": self._snapshot.device_errors,
        }


async def run_forever(service: AnalyserService, interval_s: float, stop: asyncio.Event) -> None:
    """Runs a cycle every `interval_s` until `stop` is set. Never dies on a failed cycle."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(service.run_cycle)  # keeps the API responsive during a cycle
        except Exception as exc:
            service.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("analyser cycle failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


class Envelope(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    meta: dict[str, Any] | None = None


class StateFilter(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ALL = "all"


FromMs = Annotated[int | None, Query(alias="from", ge=0, description="epoch ms")]
ToMs = Annotated[int | None, Query(alias="to", ge=0, description="epoch ms")]
DeviceId = Annotated[str | None, Query(pattern=DEVICE_ID_PATTERN)]


def _envelope(data: Any, **meta: Any) -> Envelope:
    return Envelope(success=True, data=data, meta=meta or None)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=Envelope(success=False, error=message).model_dump())


def _time_range(service: AnalyserService, from_ms: int | None, to_ms: int | None) -> tuple[int, int]:
    """[from, to] in epoch ms; defaults to the last 5 minutes at the source clock."""
    to_ms = service.snapshot.now_ms if to_ms is None else to_ms
    from_ms = to_ms - DEFAULT_RANGE_MS if from_ms is None else from_ms
    max_range_ms = int(service.settings.max_query_range_s * 1000)
    if from_ms >= to_ms:
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")
    if to_ms - from_ms > max_range_ms:
        raise HTTPException(status_code=400, detail=f"time range exceeds {max_range_ms // 1000} s")
    return from_ms, to_ms


def _register_routes(app: FastAPI, service: AnalyserService) -> None:
    @app.get("/health", response_model=Envelope)
    def health() -> JSONResponse:
        data = service.health()
        return JSONResponse(status_code=200 if data["status"] == "ok" else 503, content=_envelope(data).model_dump())

    @app.get("/api/devices", response_model=Envelope)
    def devices() -> Envelope:
        views = service.snapshot.device_views()
        online = sum(1 for v in views if v["online"])
        return _envelope(
            views,
            total=len(views),
            online=online,
            offline=len(views) - online,
            with_open_alerts=sum(1 for v in views if v["open_alerts"]),
        )

    @app.get("/api/telemetry", response_model=Envelope)
    def telemetry(from_ms: FromMs = None, to_ms: ToMs = None, device_id: DeviceId = None) -> Envelope:
        from_ms, to_ms = _time_range(service, from_ms, to_ms)
        rows = service.snapshot.telemetry(device_id, from_ms, to_ms)
        return _envelope(rows, total=len(rows), **{"from": from_ms, "to": to_ms})

    @app.get("/api/alerts", response_model=Envelope)
    def alerts(from_ms: FromMs = None, to_ms: ToMs = None, state: StateFilter = StateFilter.ALL) -> Envelope:
        from_ms, to_ms = _time_range(service, from_ms, to_ms)
        rows = service.snapshot.alerts_between(from_ms, to_ms)
        if state is not StateFilter.ALL:
            rows = [a for a in rows if a["state"] == state.value]
        open_rows = [a for a in rows if a["state"] == "open"]
        return _envelope(
            rows,
            total=len(rows),
            open=len(open_rows),
            critical=sum(1 for a in open_rows if a["severity"] == "critical"),
            **{"from": from_ms, "to": to_ms},
        )


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(p) for p in err["loc"]) for err in exc.errors())
        return _error(422, f"invalid parameters: {fields}")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path)
        return _error(500, "internal error")


def create_app(service: AnalyserService, run_loop: bool = False) -> FastAPI:
    """Builds the API. With `run_loop`, analysis cycles run in the background while the app is up."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if not run_loop:
            yield
            return
        stop = asyncio.Event()
        task = asyncio.create_task(run_forever(service, service.settings.poll_interval_s, stop))
        try:
            yield
        finally:
            stop.set()
            await task

    app = FastAPI(title="Scenario Analyzer", version="0.1.0", lifespan=lifespan)
    _register_routes(app, service)
    _register_error_handlers(app)
    return app
