"""
Entry point: analysis loop + JSON API in one process.

    ANALYSER_REPLAY_PATH=../Analyser/data/replay/ref/batches.jsonl ANALYSER_REPLAY_MODE=realtime \
    uv run python -m analyser

All settings come from ANALYSER_* environment variables (see config.py).
"""

import logging
import os
import sys

import uvicorn

from analyser.alerts import AlertSink, JsonlAlertSink, LogAlertSink
from analyser.api import AnalyserService, create_app
from analyser.config import ConfigError, Settings
from analyser.source import FileReplaySource

log = logging.getLogger("analyser")

EXIT_CONFIG_ERROR = 2


def build_service(settings: Settings) -> AnalyserService:
    source = FileReplaySource(settings.replay_path, settings.replay_mode, settings.replay_start_offset_s)
    sinks: list[AlertSink] = [LogAlertSink()]
    if settings.alerts_path:
        sinks.append(JsonlAlertSink(settings.alerts_path))
    return AnalyserService(source, settings, sinks)


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in logging.getLevelNamesMapping():
        print(f"cannot start analyser: unknown LOG_LEVEL {log_level!r}", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
        service = build_service(settings)
    except (ConfigError, OSError, ValueError) as exc:  # bad settings, unreadable replay or alerts path
        log.error("cannot start analyser: %s", exc)
        sys.exit(EXIT_CONFIG_ERROR)

    log.info(
        "analyser started: replay=%s mode=%s poll=%.0fs api=%s:%d",
        settings.replay_path,
        settings.replay_mode,
        settings.poll_interval_s,
        settings.api_host,
        settings.api_port,
    )
    app = create_app(service, run_loop=True)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level=log_level.lower())


if __name__ == "__main__":
    main()
