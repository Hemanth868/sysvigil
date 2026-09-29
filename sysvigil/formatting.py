"""Plain-text formatting shared by the dashboard and `--once`, without Textual."""

from __future__ import annotations

from .collector import Metric, Snapshot


def duration(seconds: float) -> str:
    minutes = int(seconds) // 60
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def display_value(label: str, metric: Metric) -> str:
    if metric.value is None or metric.value == "":
        return "Fan speed unavailable" if label == "Fan speed" else f"unavailable ({metric.unit})"
    if metric.unit == "s" and isinstance(metric.value, (int, float)):
        return duration(metric.value)
    if isinstance(metric.value, float):
        return f"{metric.value:.2f} {metric.unit}"
    if metric.unit in ("state", "device"):
        return str(metric.value)
    return f"{metric.value} {metric.unit}"


def metric_groups(metrics: dict[str, Metric]) -> tuple[list[str], list[str]]:
    main = [name for name in metrics if name.startswith(("CPU usage", "RAM ", "Swap ", "Root disk ", "Disk ", "Network "))]
    return main, [name for name in metrics if name not in main]


def display_metrics(snapshot: Snapshot) -> dict[str, Metric]:
    """Replace missing fan provider rows with one truthful status."""
    result = {name: metric for name, metric in snapshot.metrics.items()
              if not (name.startswith("Fan ") or name.startswith("MSI EC "))}
    rpm = [(name, metric) for name, metric in snapshot.metrics.items()
           if name.startswith("Fan ") and metric.value is not None]
    ec = [(name, metric) for name, metric in snapshot.metrics.items()
          if name.startswith("MSI EC ") and metric.value is not None]
    for name, metric in rpm + ec:
        result[name] = metric
    if not rpm:
        sources = [metric.source for name, metric in snapshot.metrics.items()
                   if name.startswith(("Fan ", "MSI EC "))]
        result["Fan speed"] = Metric(None, "RPM", "; ".join(sources))
    return result
