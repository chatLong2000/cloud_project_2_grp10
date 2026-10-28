"""
Entry point: analysis loop + JSON API in one process.

    ANALYSER_REPLAY_PATH=../Analyser/data/replay/ref/batches.jsonl ANALYSER_REPLAY_MODE=realtime \
    uv run python -m analyser

    ANALYSER_SOURCE=timestream uv run python -m analyser    # AWS credentials from the usual chain

All settings come from ANALYSER_* environment variables (see config.py).
"""

import logging
import os
import sys

import uvicorn

from analyser.alerts import AlertSink, JsonlAlertSink, LogAlertSink, SnsAlertSink, TimestreamAlertSink
from analyser.api import AnalyserService, create_app
from analyser.cloudwatch import CloudWatchLogsHandler
from analyser.config import ConfigError, Settings, SourceKind
from analyser.source import FileReplaySource, TelemetrySource
from analyser.timestream import TimestreamSource, query_client

log = logging.getLogger("analyser")

EXIT_CONFIG_ERROR = 2


def build_source(settings: Settings) -> TelemetrySource:
    if settings.source is SourceKind.TIMESTREAM:
        return TimestreamSource(
            query_client(settings.timestream_region),
            settings.timestream_database,
            settings.timestream_table,
            settings.timestream_lookback_s,
            settings.timestream_backfill_s,
        )
    return FileReplaySource(settings.replay_path, settings.replay_mode, settings.replay_start_offset_s)


def describe_source(settings: Settings) -> str:
    if settings.source is SourceKind.TIMESTREAM:
        return f"timestream={settings.timestream_database}.{settings.timestream_table} ({settings.timestream_region})"
    return f"replay={settings.replay_path} mode={settings.replay_mode}"


def build_service(settings: Settings) -> AnalyserService:
    source = build_source(settings)
    sinks: list[AlertSink] = [LogAlertSink()]
    if settings.alerts_path:
        sinks.append(JsonlAlertSink(settings.alerts_path))
    if settings.source is SourceKind.TIMESTREAM and settings.timestream_write_alerts:
        sinks.append(
            TimestreamAlertSink(
                database=settings.timestream_database,
                table=settings.timestream_alerts_table,
                region=settings.timestream_region,
            )
        )
    if settings.sns_topic_arn:
        sinks.append(
            SnsAlertSink(
                settings.sns_topic_arn,
                region=settings.timestream_region,
                notify_resolved=settings.sns_notify_resolved,
                min_severity=settings.sns_min_severity,
            )
        )
    return AnalyserService(source, settings, sinks)


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in logging.getLevelNamesMapping():
        print(f"cannot start analyser: unknown LOG_LEVEL {log_level!r}", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
        if settings.cloudwatch_enabled:
            cw_handler = CloudWatchLogsHandler(
                log_group_name=settings.cloudwatch_log_group,
                log_stream_name=settings.cloudwatch_log_stream,
                region=settings.cloudwatch_region,
                retention_days=settings.cloudwatch_retention_days,
            )
            cw_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            cw_handler.setLevel(log_level)
            logging.getLogger().addHandler(cw_handler)
            log.info(
                "cloudwatch logging enabled: group=%s retention=%dj region=%s",
                settings.cloudwatch_log_group,
                settings.cloudwatch_retention_days,
                settings.cloudwatch_region,
            )
        service = build_service(settings)
    except (ConfigError, OSError, ValueError) as exc:  # bad settings, unreadable replay or alerts path
        log.error("cannot start analyser: %s", exc)
        sys.exit(EXIT_CONFIG_ERROR)

    log.info(
        "analyser started: %s poll=%.0fs api=%s:%d",
        describe_source(settings),
        settings.poll_interval_s,
        settings.api_host,
        settings.api_port,
    )
    app = create_app(service, run_loop=True)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level=log_level.lower())


if __name__ == "__main__":
    main()
