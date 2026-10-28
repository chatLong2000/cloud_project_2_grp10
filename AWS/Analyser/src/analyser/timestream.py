"""
Telemetry source on Amazon Timestream for LiveAnalytics (the AWS database).

The Ingestor Lambda writes one multi-measure record per reading:
    dimensions   device_id, edge_id
    measure_name 'telemetry_metrics'
    time         the reading timestamp
    measures     uptime_ms, temperature_c, power_consumption_mw,
                 heap_free_bytes, device_status, error_code

fetch() reads everything newer than the previous successful fetch minus
`lookback_s` (the edge may deliver a batch up to ~5 min late), and keeps per
device only the readings newer than the last one it already returned, so the
overlapping queries never feed the engine twice. The first fetch reads
`backfill_s` instead, to refill the sliding window after a (re)start. The
clock is the wall clock. A failed query raises: the service reports the cycle
as failed and the next fetch queries the same range again.
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import boto3

from analyser.models import Reading, ValidationError, parse_reading
from analyser.source import Fetched, wall_clock_ms

log = logging.getLogger("analyser.timestream")

MEASURE_NAME = "telemetry_metrics"
MEASURE_COLUMNS = (
    "uptime_ms",
    "temperature_c",
    "power_consumption_mw",
    "heap_free_bytes",
    "device_status",
    "error_code",
)


def query_client(region: str) -> Any:
    return boto3.client("timestream-query", region_name=region)


def build_query(database: str, table: str, since_ms: int) -> str:
    """Names are validated by config (no quotes); since_ms is an int, never user text."""
    columns = ", ".join(("device_id", *MEASURE_COLUMNS))
    return (
        f"SELECT {columns}, to_milliseconds(time) AS timestamp "
        f'FROM "{database}"."{table}" '
        f"WHERE measure_name = '{MEASURE_NAME}' AND time > from_milliseconds({int(since_ms)})"
    )


def row_to_dict(columns: Iterable[str], row: Mapping[str, Any]) -> dict[str, Any]:
    """A Timestream row as {column: value}; NULL becomes None (and fails validation)."""
    return {name: cell.get("ScalarValue") for name, cell in zip(columns, row["Data"], strict=True)}


class TimestreamSource:
    def __init__(
        self,
        client: Any,
        database: str,
        table: str,
        lookback_s: float = 600.0,
        backfill_s: float = 900.0,
        wall_clock: Callable[[], int] = wall_clock_ms,
    ):
        self._client = client
        self._database = database
        self._table = table
        self._lookback_ms = int(lookback_s * 1000)
        self._backfill_ms = int(backfill_s * 1000)
        self._wall_clock = wall_clock
        self._last_fetch_ms: int | None = None
        self._last_seen: Mapping[str, int] = {}
        self.rejected_rows = 0
        log.info("reading Timestream %s.%s (lookback %ds)", database, table, lookback_s)

    def fetch(self) -> Fetched:
        now_ms = self._wall_clock()
        since_ms = self._since(now_ms)
        readings = self._query(since_ms)
        fresh = tuple(r for r in readings if r.timestamp > self._last_seen.get(r.device_id, -1))
        self._last_seen = {**self._last_seen, **_latest_by_device(fresh)}
        self._last_fetch_ms = now_ms
        log.debug("Timestream: %d rows since %d, %d new", len(readings), since_ms, len(fresh))
        return Fetched(readings=fresh, now_ms=now_ms)

    def _since(self, now_ms: int) -> int:
        oldest = now_ms - self._backfill_ms
        if self._last_fetch_ms is None:
            return oldest
        return max(oldest, self._last_fetch_ms - self._lookback_ms)

    def _query(self, since_ms: int) -> list[Reading]:
        query = build_query(self._database, self._table, since_ms)
        readings, rejected, first_error = [], 0, None
        for page in self._client.get_paginator("query").paginate(QueryString=query):
            columns = [c["Name"] for c in page["ColumnInfo"]]
            for row in page["Rows"]:
                try:
                    readings.append(parse_reading(row_to_dict(columns, row)))
                except ValidationError as exc:
                    rejected += 1
                    first_error = first_error or exc
        if rejected:
            self.rejected_rows += rejected
            log.warning("Timestream: skipped %d invalid rows (first: %s)", rejected, first_error)
        return readings


def _latest_by_device(readings: Iterable[Reading]) -> dict[str, int]:
    latest: dict[str, int] = {}
    for r in readings:
        latest[r.device_id] = max(r.timestamp, latest.get(r.device_id, r.timestamp))
    return latest
