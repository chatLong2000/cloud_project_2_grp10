"""
Plotting helpers shared by the analyser notebooks (presentation only, no detection logic).

Colors follow one rule per job: the measured series is blue, each ground truth
scenario has a fixed categorical color, alert severities use the reserved status
colors (always with a label), and the device status is drawn without color.
"""

from collections.abc import Iterable, Sequence

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_MUTED = "#52514e"
GRID = "#e6e5e0"
SERIES = "#2a78d6"
SECOND_SERIES = "#eb6834"
GROUND_TRUTH = "#c3c2b7"
GAP = pd.Timedelta(seconds=5)  # sampling is every 2 s: a longer gap is missing data

SCENARIO_COLORS = {
    "thermal_runaway": "#eb6834",
    "memory_leak_fota": "#1baf7a",
    "out_of_memory": "#eda100",
    "silent_device": "#e87ba4",
}
SEVERITY_COLORS = {"warning": "#fab219", "critical": "#d03b3b"}
STATUS_ORDER = ["off", "booting", "preheat", "standby", "brewing", "updating", "degraded", "error"]
SCENARIO_LABELS = {
    "thermal_runaway": "emballement thermique",
    "memory_leak_fota": "fuite mémoire (FOTA)",
    "out_of_memory": "out of memory",
    "silent_device": "machine muette",
    "device_offline": "machine hors ligne",
}


def apply_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "axes.edgecolor": GRID,
            "axes.labelcolor": TEXT_MUTED,
            "axes.titlecolor": TEXT,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": TEXT_MUTED,
            "ytick.color": TEXT_MUTED,
            "legend.frameon": False,
            "lines.linewidth": 1.4,
            "figure.dpi": 110,
        }
    )


def to_utc(ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True)


def readings_frame(readings: Iterable) -> pd.DataFrame:
    df = pd.DataFrame([r.model_dump(mode="json") for r in readings])
    df["time"] = to_utc(df["timestamp"])
    df["power_w"] = df["power_consumption_mw"] / 1000
    return df.sort_values(["device_id", "timestamp"]).reset_index(drop=True)


def episodes_frame(episodes: Iterable) -> pd.DataFrame:
    df = pd.DataFrame([{**e.__dict__, "scenario": e.scenario.value} for e in episodes])
    if df.empty:
        return pd.DataFrame(columns=["device_id", "scenario", "start", "end", "complete"])
    df["start_time"], df["end_time"] = to_utc(df["start"]), to_utc(df["end"])
    return df


def alerts_frame(alerts: Iterable) -> pd.DataFrame:
    df = pd.DataFrame([a.model_dump(mode="json") for a in alerts])
    if df.empty:
        return df
    df["opened_time"] = to_utc(df["opened_at"])
    df["resolved_time"] = to_utc(df["resolved_at"].fillna(df["updated_at"]))
    return df


def scores_frame(scores: Iterable) -> pd.DataFrame:
    rows = [{**s.__dict__, "scenario": SCENARIO_LABELS[s.scenario.value]} for s in scores]
    return pd.DataFrame(rows).set_index("scenario")


def sweep_frame(results: Iterable, parameter: str, scenario: str) -> pd.DataFrame:
    """One row per tested value, with the scores of one scenario."""
    rows = []
    for value, scores in results:
        score = next(s for s in scores if s.scenario.value == scenario)
        rows.append({parameter: value, **{k: v for k, v in score.__dict__.items() if k != "scenario"}})
    return pd.DataFrame(rows)


def _break_at_gaps(readings: pd.DataFrame, columns: Sequence[str], gap: pd.Timedelta = GAP) -> pd.DataFrame:
    """Inserts an empty point in every data gap so lines are not drawn across missing readings."""
    after_gap = readings["time"].diff() > gap
    breaks = readings.loc[after_gap, ["time"]].assign(time=lambda d: d["time"] - pd.Timedelta(milliseconds=1))
    breaks[list(columns)] = float("nan")
    return pd.concat([readings, breaks]).sort_values("time")


def _time_axis(ax: plt.Axes) -> None:
    locator = mdates.AutoDateLocator(tz="UTC")
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz="UTC"))


def _shade_episodes(ax: plt.Axes, episodes: pd.DataFrame) -> None:
    for row in episodes.itertuples():
        ax.axvspan(row.start_time, row.end_time, color=SCENARIO_COLORS[row.scenario], alpha=0.22, lw=0)


def _episode_legend(episodes: pd.DataFrame) -> list[Patch]:
    return [
        Patch(color=SCENARIO_COLORS[s], alpha=0.4, label=f"vérité terrain : {SCENARIO_LABELS[s]}")
        for s in SCENARIO_COLORS
        if s in set(episodes.get("scenario", []))
    ]


def _alert_strip(ax: plt.Axes, alerts: pd.DataFrame) -> None:
    scenarios = [s for s in SCENARIO_LABELS if not alerts.empty and s in set(alerts["scenario"])]
    for y, scenario in enumerate(scenarios):
        for row in alerts[alerts["scenario"] == scenario].itertuples():
            ax.plot([row.opened_time, row.resolved_time], [y, y], color=SEVERITY_COLORS[row.severity], lw=6)
            ax.plot(row.opened_time, y, marker="|", ms=12, color=SEVERITY_COLORS[row.severity])
    ax.set_yticks(range(len(scenarios)), [SCENARIO_LABELS[s] for s in scenarios])
    ax.set_ylim(-0.7, max(len(scenarios), 1) - 0.3)
    ax.set_title("Alertes détectées", fontsize=10)


def plot_device(
    readings: pd.DataFrame,
    episodes: pd.DataFrame,
    alerts: pd.DataFrame | None = None,
    title: str = "",
    window: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    shade: Sequence[str] | None = None,
) -> plt.Figure:
    """Temperature, heap, power and status of one device, one measure per panel.

    `window` zooms on a time range; `shade` limits the shaded ground truth scenarios.
    """
    if shade is not None:
        episodes = episodes[episodes["scenario"].isin(shade)]
    if window is not None:
        readings = readings[(readings["time"] >= window[0]) & (readings["time"] <= window[1])]
        episodes = episodes[(episodes["end_time"] >= window[0]) & (episodes["start_time"] <= window[1])]
        if alerts is not None and not alerts.empty:
            alerts = alerts[(alerts["resolved_time"] >= window[0]) & (alerts["opened_time"] <= window[1])]
    panels = [("temperature_c", "Température (°C)"), ("heap_free_bytes", "Heap libre (octets)"), ("power_w", "Puissance (W)")]
    rows = len(panels) + 1 + (alerts is not None)
    heights = [3, 3, 2, 2] + ([1.6] if alerts is not None else [])
    fig, axes = plt.subplots(rows, 1, sharex=True, figsize=(12, 1.6 * sum(heights) / 2 + 1.5),
                             gridspec_kw={"height_ratios": heights})
    lines = _break_at_gaps(readings, [c for c, _ in panels])
    for ax, (column, label) in zip(axes, panels):
        ax.plot(lines["time"], lines[column], color=SERIES, lw=1.2)
        ax.set_title(label, fontsize=10)
        _shade_episodes(ax, episodes)
    status_ax = axes[len(panels)]
    codes = lines["device_status"].map({s: i for i, s in enumerate(STATUS_ORDER)})
    status_ax.step(lines["time"], codes, where="post", color=TEXT_MUTED, lw=1)
    status_ax.set_yticks(range(len(STATUS_ORDER)), STATUS_ORDER, fontsize=8)
    status_ax.set_title("Statut", fontsize=10)
    _shade_episodes(status_ax, episodes)
    if alerts is not None:
        _alert_strip(axes[-1], alerts)
    handles = _episode_legend(episodes) + [
        Patch(color=c, label=f"alerte {s}") for s, c in SEVERITY_COLORS.items() if alerts is not None
    ]
    if handles:
        fig.legend(handles=handles, loc="upper right", ncols=3, fontsize=8)
    if window is not None:
        axes[-1].set_xlim(window)
    _time_axis(axes[-1])
    fig.suptitle(title, x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return fig


def plot_small_multiples(
    readings: pd.DataFrame, episodes: pd.DataFrame, column: str, label: str, scenarios: Sequence[str]
) -> plt.Figure:
    """One panel per device, same y scale, with the selected ground truth scenarios shaded."""
    devices = sorted(readings["device_id"].unique())
    cols = 2
    fig, axes = plt.subplots((len(devices) + 1) // cols, cols, sharex=True, sharey=True,
                             figsize=(12, 1.5 * ((len(devices) + 1) // cols) + 1), squeeze=False)
    for ax, device in zip(axes.flat, devices):
        dev = readings[readings["device_id"] == device]
        ax.plot(dev["time"], dev[column], color=SERIES, lw=0.8)
        _shade_episodes(ax, episodes[(episodes["device_id"] == device) & episodes["scenario"].isin(scenarios)])
        ax.set_title(device, fontsize=9)
    for ax in axes.flat[len(devices):]:
        ax.set_visible(False)
    _time_axis(axes.flat[0])
    fig.legend(handles=[h for h in _episode_legend(episodes) if any(SCENARIO_LABELS[s] in h.get_label() for s in scenarios)],
               loc="upper right", fontsize=8)
    fig.suptitle(label, x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig


def plot_timelines(episodes: pd.DataFrame, alerts: pd.DataFrame, scenarios: Sequence[str]) -> plt.Figure:
    """Per scenario: ground truth episodes (gray bars) vs alert openings (markers), one row per device."""
    devices = sorted(set(episodes["device_id"]) | (set(alerts["device_id"]) if not alerts.empty else set()))
    fig, axes = plt.subplots(len(scenarios), 1, sharex=True, figsize=(12, 1 + 0.32 * len(devices) * len(scenarios)))
    for ax, scenario in zip(axes, scenarios):
        eps = episodes[episodes["scenario"] == scenario]
        hits = alerts[alerts["scenario"] == scenario] if not alerts.empty else alerts
        for y, device in enumerate(devices):
            for row in eps[eps["device_id"] == device].itertuples():
                width = max(row.end_time - row.start_time, pd.Timedelta(seconds=60))
                ax.barh(y, width, left=row.start_time, height=0.6, color=GROUND_TRUTH, lw=0)
            dev_hits = hits[hits["device_id"] == device] if not hits.empty else hits
            if not dev_hits.empty:
                ax.plot(dev_hits["opened_time"], [y] * len(dev_hits), "D", ms=5,
                        color=SCENARIO_COLORS[scenario], markeredgecolor=SURFACE, markeredgewidth=1)
        ax.set_yticks(range(len(devices)), devices, fontsize=8)
        ax.set_title(SCENARIO_LABELS[scenario], fontsize=10)
        ax.grid(axis="y", visible=False)
    _time_axis(axes[-1])
    fig.legend(handles=[Patch(color=GROUND_TRUTH, label="épisode (vérité terrain)"),
                        plt.Line2D([], [], marker="D", ls="", color=TEXT_MUTED, label="ouverture d'alerte")],
               loc="upper right", ncols=2, fontsize=8)
    fig.suptitle("Détections vs vérité terrain", x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig


def plot_sweep(frame: pd.DataFrame, parameter: str, label: str, default: float) -> plt.Figure:
    """Recall/precision (shared 0-1 scale) and detection latency on separate panels."""
    fig, (quality, latency) = plt.subplots(1, 2, figsize=(12, 3.4))
    for column, color, name in (("recall", SERIES, "rappel"), ("precision", SECOND_SERIES, "précision")):
        quality.plot(frame[parameter], frame[column], marker="o", ms=4, color=color, label=name)
    quality.set_ylim(-0.05, 1.08)
    quality.set_title("Qualité de détection", fontsize=10)
    quality.legend(fontsize=8, loc="lower left")
    latency.plot(frame[parameter], frame["median_latency_s"], marker="o", ms=4, color=SERIES)
    latency.set_title("Délai de détection médian (s)", fontsize=10)
    integer = pd.api.types.is_integer_dtype(frame[parameter])
    for ax in (quality, latency):
        ax.axvline(default, color=TEXT_MUTED, ls="--", lw=1)
        ax.set_xlabel(label)
        if integer:
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    quality.annotate("défaut", (default, 1.04), xytext=(4, 0), textcoords="offset points", fontsize=8, color=TEXT_MUTED)
    fig.tight_layout()
    return fig
