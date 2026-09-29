"""Command-line entry point."""

from __future__ import annotations

import argparse
from datetime import datetime
import time

from . import __version__
from .collector import Collector, Snapshot
from .formatting import display_metrics, display_value, metric_groups


def format_snapshot(snapshot: Snapshot) -> str:
    lines = [f"Sampled: {datetime.fromtimestamp(snapshot.timestamp).astimezone():%Y-%m-%d %H:%M:%S %Z}"]
    metrics = display_metrics(snapshot)
    main, others = metric_groups(metrics)
    for title, names in (("Resources", main), ("CPU cores, sensors, GPU, battery, and pressure", others)):
        lines.append(f"\n{title}:")
        for name in names:
            metric = metrics[name]
            lines.append(f"  {name}: {display_value(name, metric)} [source: {metric.source}]")
    lines.append("\nTop processes (CPU over sample interval):")
    lines.append("  PID [source: /proc/PID/stat] | Process [source: /proc/PID/stat] | CPU % [source: /proc/PID/stat] | RAM MiB [source: /proc/PID/statm] | I/O MiB/s [source: /proc/PID/io]")
    for row in snapshot.processes[:8]:
        cpu = f"{row.cpu_percent:.1f} %" if row.cpu_percent is not None else "unavailable"
        io = f"{row.io_mib_s:.2f} MiB/s" if row.io_mib_s is not None else "unavailable"
        lines.append(f"  {row.pid} | {row.name} | {cpu} | {row.memory_mib:.1f} MiB | {io}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sysvigil", description="Read-only Linux resource monitor")
    parser.add_argument("--once", action="store_true", help="print a one-second snapshot and exit")
    parser.add_argument("--version", action="version", version=f"sysvigil {__version__}")
    args = parser.parse_args()
    if args.once:
        collector = Collector()
        collector.prime()
        time.sleep(1)
        print(format_snapshot(collector.sample()))
    else:
        # Imported here so `--once` does not need Textual.
        from .ui import MonitorApp

        MonitorApp().run()
