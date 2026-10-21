"""
Settings, read from ANALYSER_* environment variables.

Detection thresholds default to the simulator configuration
(simulator/config.yaml) and to doc/G10-architecture-V2.md §4. An invalid
value fails fast at startup, naming the offending variable.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")

PREFIX = "ANALYSER_"


class ConfigError(ValueError):
    pass


class ReplayMode(StrEnum):
    INSTANT = "instant"  # whole file at once (notebooks, tests)
    REALTIME = "realtime"  # batches released as the wall clock reaches them (Grafana demo)


def _number(name: str, raw: str, cast: Callable[[str], T]) -> T:
    try:
        return cast(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid {cast.__name__}") from None


def _positive_float(name: str, raw: str) -> float:
    value = _number(name, raw, float)
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {raw!r}")
    return value


def _non_negative_float(name: str, raw: str) -> float:
    value = _number(name, raw, float)
    if value < 0:
        raise ConfigError(f"{name} must be >= 0, got {raw!r}")
    return value


def _positive_int(name: str, raw: str) -> int:
    value = _number(name, raw, int)
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {raw!r}")
    return value


def _replay_mode(name: str, raw: str) -> ReplayMode:
    try:
        return ReplayMode(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(m.value for m in ReplayMode)
        raise ConfigError(f"{name}={raw!r} must be one of: {allowed}") from None


@dataclass(frozen=True)
class Thresholds:
    setpoint_c: float = 94.0
    thermal_delta_c: float = 4.0
    thermal_consecutive: int = 3
    thermal_critical_c: float = 112.0
    leak_min_drop_bytes: int = 2_000
    leak_min_samples: int = 10
    leak_streak: int = 2
    oom_critical_free_bytes: int = 5_000
    silent_gap_s: float = 10.0
    offline_after_s: float = 300.0

    @property
    def thermal_warn_c(self) -> float:
        return self.setpoint_c + self.thermal_delta_c

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Thresholds":
        values: dict[str, Any] = {}
        for f in fields(cls):
            name = PREFIX + f.name.upper()
            if name in env:
                parse = _positive_int if f.type in (int, "int") else _positive_float
                values[f.name] = parse(name, env[name])
        return cls(**values)


@dataclass(frozen=True)
class Settings:
    replay_path: str
    replay_mode: ReplayMode = ReplayMode.INSTANT
    replay_start_offset_s: float = 0.0
    poll_interval_s: float = 30.0
    window_s: float = 3_600.0
    max_alerts: int = 1_000
    max_query_range_s: float = 86_400.0
    alerts_path: str | None = None
    api_host: str = "127.0.0.1"  # the container image sets 0.0.0.0
    api_port: int = 8_000
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        replay_path = env.get(PREFIX + "REPLAY_PATH", "").strip()
        if not replay_path:
            raise ConfigError(f"{PREFIX}REPLAY_PATH is required (path to a batches.jsonl file)")

        def get(name: str, parse: Callable[[str, str], T], default: T) -> T:
            key = PREFIX + name
            return parse(key, env[key]) if key in env else default

        return cls(
            replay_path=replay_path,
            replay_mode=get("REPLAY_MODE", _replay_mode, ReplayMode.INSTANT),
            replay_start_offset_s=get("REPLAY_START_OFFSET_S", _non_negative_float, 0.0),
            poll_interval_s=get("POLL_INTERVAL_S", _positive_float, 30.0),
            window_s=get("WINDOW_S", _positive_float, 3_600.0),
            max_alerts=get("MAX_ALERTS", _positive_int, 1_000),
            max_query_range_s=get("MAX_QUERY_RANGE_S", _positive_float, 86_400.0),
            alerts_path=env.get(PREFIX + "ALERTS_PATH") or None,
            api_host=env.get(PREFIX + "API_HOST", "127.0.0.1"),
            api_port=get("API_PORT", _positive_int, 8_000),
            thresholds=Thresholds.from_env(env),
        )
