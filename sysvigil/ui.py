"""Responsive Textual dashboard; all measurements come from the collector."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
import math
import os
import re
import sys

from rich.color import Color
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.driver import Driver
from textual.drivers.headless_driver import HeadlessDriver
from textual.markup import escape
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from .collector import Collector, Metric, ProcessRow, Snapshot
from .formatting import display_metrics, display_value, duration


SPARKS = "▁▂▃▄▅▆▇█"
BLOCKS = " ▁▂▃▄▅▆▇█"
PROCESS_ROWS = 40
# Graphs draw one column per sample, so wide cards show more than a minute.
HISTORY_SECONDS = 300
# Bordered cards with graphs need this many columns across four cards and
# this many rows per card; smaller terminals get the compact layout.
WIDE_MIN_WIDTH = 140
CARD_MIN_HEIGHT = 8

# The look: one signature violet carries every graph over a dark ink surface,
# so any other colour means a value needs attention. Those status colours are
# reserved for that and always come with a shape (● ▲ ■) and a label.
PAGE = "#0c0e16"
SURFACE = "#121521"
TEXT = "#eceef8"
LABEL = "#a6abc3"
MUTED = "#7d839f"
FAINT = "#3a3f55"
ACCENT = "#9085e9"
GOOD = "#0ca30c"
WARN = "#fab219"
CRIT = "#d03b3b"
STATUS = {"warn": ("▲", WARN), "crit": ("■", CRIT)}
GLYPH = "◆"
# (warn, crit) thresholds; a descending pair means lower is worse.
USAGE_LEVELS = (80.0, 95.0)
TEMP_LEVELS = (85.0, 95.0)
DISK_LEVELS = (90.0, 95.0)
BATTERY_LEVELS = (20.0, 10.0)
CARD_KEYS = ("cpu", "memory", "gpu1", "gpu2", "battery", "thermal", "disk", "net")

COMPACT_COLUMNS = (("PID", "pid"), ("Process", "name"), ("CPU %", "cpu"),
                   ("RAM", "memory"), ("I/O", "io"))
WIDE_COLUMNS = (("PID", "pid"), ("Process", "name"), ("User", "user"), ("CPU %", "cpu"),
                ("RAM", "memory"), ("I/O", "io"), ("Command", "command"))
# Backwards-compatible name for the compact column set.
PROCESS_COLUMNS = COMPACT_COLUMNS


def mix(color: str, base: str, amount: float) -> str:
    """`amount` of `color` over `base`, the terminal's stand-in for opacity."""
    top, bottom = (Color.parse(c).get_truecolor() for c in (color, base))
    return "#" + "".join(f"{round(a * amount + b * (1 - amount)):02x}" for a, b in zip(top, bottom))


BORDER = mix(ACCENT, SURFACE, 0.28)
TRACK = mix(TEXT, SURFACE, 0.12)


def human_rate(mib_s: float | None) -> str:
    if mib_s is None:
        return "—"
    size = mib_s * 2**20
    for unit in ("B/s", "KiB/s", "MiB/s"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B/s" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} GiB/s"


def human_mib(mib: float | None) -> str:
    if mib is None:
        return "—"
    if mib >= 1024:
        return f"{mib / 1024:.1f} GiB"
    return f"{mib:.1f} MiB" if mib < 10 else f"{mib:.0f} MiB"


def metric_names(snapshot: Snapshot, group: str, metrics: dict[str, Metric] | None = None) -> list[str]:
    metrics = metrics if metrics is not None else display_metrics(snapshot)
    if group == "cores":
        return [name for name in metrics if name.startswith(("CPU usage", "CPU core ", "CPU frequency", "CPU load ", "RAM ", "Swap "))]
    if group == "sensors":
        return [name for name in metrics if name.startswith(("CPU temperature", "GPU ", "Fan ", "Battery ", "MSI EC ", "Root disk ", "Disk ", "Network "))]
    if group == "pressure":
        return [name for name in metrics if " pressure " in name]
    return list(metrics)


def metric_table(snapshot: Snapshot, names: list[str], sources: bool = False,
                 metrics: dict[str, Metric] | None = None) -> Table:
    """Detail views; percentages get a bar so per-core load reads at a glance."""
    metrics = metrics if metrics is not None else display_metrics(snapshot)
    table = Table(show_header=True, expand=True, box=None, pad_edge=False,
                  header_style=f"bold {LABEL}", padding=(0, 2))
    table.add_column("Measurement", no_wrap=True, style=LABEL)
    table.add_column("Value", no_wrap=True, style=f"bold {TEXT}", justify="right")
    if sources:
        table.add_column("Source", style=FAINT)
    else:
        table.add_column("", no_wrap=True, ratio=1)
    for name in names:
        metric = metrics[name]
        cells: list[str | Text] = [name, display_value(name, metric)]
        if sources:
            cells.append(metric.source)
        elif metric.unit == "%" and isinstance(metric.value, (int, float)):
            cells.append(meter(metric.value, 30))
        else:
            cells.append("")
        table.add_row(*cells)
    if sources:
        table.add_row("Process PID/name/CPU", "% or ID", "/proc/PID/stat")
        table.add_row("Process RAM", "MiB", "/proc/PID/statm")
        table.add_row("Process I/O", "MiB/s", "/proc/PID/io")
        table.add_row("Process user/command", "text", "/proc/PID/status, /proc/PID/cmdline")
    return table


class History:
    """Timestamped values covering the last `seconds`, not merely N samples."""

    def __init__(self, seconds: float = 60) -> None:
        self.seconds = seconds
        self._values: dict[str, deque[tuple[float, float | None]]] = defaultdict(deque)

    def add(self, snapshot: Snapshot) -> None:
        now = snapshot.timestamp
        for name in set(self._values) | set(snapshot.metrics):
            metric = snapshot.metrics.get(name)
            value = metric.value if metric is not None else None
            number = float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None
            values = self._values[name]
            values.append((now, number))
            while values and values[0][0] < now - self.seconds:
                values.popleft()

    def values(self, name: str, seconds: float | None = None) -> list[float | None]:
        values = self._values.get(name, ())
        if seconds is None or not values:
            return [value for _, value in values]
        start = values[-1][0] - seconds
        return [value for stamp, value in values if stamp >= start]


def sparkline(values: list[float | None], width: int) -> str:
    if width <= 0:
        return ""
    if not values:
        return "·" * width
    if len(values) > width:
        reduced: list[float | None] = []
        for column in range(width):
            segment = values[column * len(values) // width:(column + 1) * len(values) // width]
            valid = [value for value in segment if value is not None]
            reduced.append(max(valid) if valid else None)
        values = reduced
    valid = [value for value in values if value is not None]
    if not valid:
        trace = "·" * len(values)
    else:
        low, high = min(valid), max(valid)
        trace = "".join("·" if value is None else SPARKS[1 if high == low else
                        min(7, max(0, round((value - low) * 7 / (high - low))))]
                        for value in values)
    return "·" * (width - len(trace)) + trace


def area_graph(values: list[float | None], width: int, height: int,
               scale: tuple[float, float] | None, accent: str,
               levels: tuple[float, float] | None = None) -> list[Text]:
    """A filled graph, one column per sample, newest on the right.

    Each column is a bright ridge (its top cell, in eighths) over a dim wash
    of the same hue. With `levels`, a sample's ridge takes the warn or crit
    colour. `scale=None` spans zero to the peak.
    """
    if width <= 0 or height <= 0:
        return []
    values = list(values[-width:])
    values = [None] * (width - len(values)) + values
    valid = [value for value in values if value is not None]
    if scale is None:
        low, high = 0.0, max(valid, default=0.0)
    else:
        low, high = scale
    if high <= low:
        high = low + 1
    steps = height * 8
    heights = [None if value is None else
               max(1, round((min(max(value, low), high) - low) * steps / (high - low)))
               for value in values]
    # Glyph and cell background share the wash, so the fill has no seams.
    wash = mix(accent, SURFACE, 0.22)
    wash = f"{wash} on {wash}"
    ridges = [tone(value, levels, normal=accent) for value in values]
    lines = []
    for row in range(height):
        base = (height - 1 - row) * 8
        line = Text()
        for level, ridge in zip(heights, ridges):
            if level is None:
                # A faint floor marks where samples will appear.
                line.append("▁" if row == height - 1 else " ", style=FAINT)
            elif level <= base:
                line.append(" ")
            elif level <= base + 8:
                line.append(BLOCKS[level - base], style=ridge)
            else:
                line.append("█", style=wash)
        lines.append(line)
    return lines


def bar(percent: float | None, width: int) -> str:
    if percent is None or not math.isfinite(percent):
        return "·" * width
    filled = round(max(0.0, min(100.0, percent)) * width / 100)
    return "━" * filled + "─" * (width - filled)


def meter(percent: float | None, width: int, levels: tuple[float, float] | None = USAGE_LEVELS) -> Text:
    """`bar` in colour: the fill in the accent or a status colour, a dim track."""
    plain = bar(percent, width)
    filled = plain.count("━")
    return Text.assemble((plain[:filled], tone(percent, levels)), (plain[filled:], TRACK))


def value(snapshot: Snapshot, name: str) -> float | None:
    metric = snapshot.metrics.get(name)
    return float(metric.value) if metric and isinstance(metric.value, (int, float)) else None


def summed_value(snapshot: Snapshot, *names: str) -> float | None:
    """A sum is unavailable when any part is, as in the summed graphs."""
    parts = [value(snapshot, name) for name in names]
    known = [part for part in parts if part is not None]
    return sum(known) if len(known) == len(parts) else None


def number(snapshot: Snapshot, name: str, digits: int = 1) -> str:
    current = value(snapshot, name)
    return f"{current:.{digits}f}" if current is not None else "—"


def severity(current: float | None, levels: tuple[float, float] | None) -> str | None:
    """"warn", "crit", or None for a value against (warn, crit) thresholds."""
    if current is None or levels is None:
        return None
    warn, crit = levels
    if warn > crit:  # lower is worse, as for battery charge
        return "crit" if current <= crit else "warn" if current <= warn else None
    return "crit" if current >= crit else "warn" if current >= warn else None


def tone(current: float | None, levels: tuple[float, float] | None = USAGE_LEVELS, *,
         normal: str = ACCENT) -> str:
    if current is None:
        return FAINT
    level = severity(current, levels)
    return STATUS[level][1] if level else normal


def flagged(text: str, level: str | None, style: str = TEXT) -> Text:
    """A value that needs attention wears its status shape and colour."""
    if level is None:
        return Text(text, style=style)
    icon, color = STATUS[level]
    return Text(f"{icon} {text}", style=f"bold {color}")


def gpu_cards(snapshot: Snapshot) -> list[str]:
    cards = {match.group(1) for name in snapshot.metrics
             if (match := re.fullmatch(r"GPU (card\d+) activity", name))}
    return sorted(cards, key=lambda item: int(item[4:]))


def gpu_title(snapshot: Snapshot, card_name: str) -> str:
    metric = snapshot.metrics.get(f"GPU {card_name} device")
    identity = str(metric.value).split(" · ")[0].split(" / ")[0] if metric and metric.value else card_name
    return f"GPU {card_name[4:]} · {identity}"


def gpu_power(snapshot: Snapshot, card_name: str) -> tuple[str, str]:
    prefix = f"GPU {card_name} "
    for name in snapshot.metrics:
        if name.startswith(prefix) and name.endswith(" power"):
            label = name.removeprefix(prefix).removesuffix(" power")
            return label.upper() if label == "PPT" else label.title(), number(snapshot, name)
    return "Power", "—"


def fan_status(snapshot: Snapshot) -> tuple[str, str]:
    rpm = [(name, metric) for name, metric in snapshot.metrics.items()
           if name.startswith("Fan ") and metric.value is not None]
    ec = [(name, metric) for name, metric in snapshot.metrics.items()
          if name.startswith("MSI EC ") and metric.value is not None]
    main = f"{rpm[0][1].value} RPM" if rpm else "Fan speed unavailable"
    detail = " · ".join(f"{name.removeprefix('MSI EC ')} {metric.value} %" for name, metric in ec)
    return main, detail


def battery_power_label(snapshot: Snapshot) -> str:
    power = value(snapshot, "Battery power")
    if power is None:
        return "Power unavailable"
    if power > 0:
        return f"Charging {power:.1f} W"
    if power < 0:
        return f"Discharging {abs(power):.1f} W"
    return "Power 0.0 W · idle"


def sorted_processes(rows: list[ProcessRow], field: str, descending: bool) -> list[ProcessRow]:
    getters = {
        "cpu": lambda row: row.cpu_percent,
        "memory": lambda row: row.memory_mib,
        "io": lambda row: row.io_mib_s,
        "pid": lambda row: row.pid,
    }
    getter = getters[field]
    known = [row for row in rows if getter(row) is not None]
    missing = [row for row in rows if getter(row) is None]
    return sorted(known, key=getter, reverse=descending) + missing


@dataclass
class CardData:
    key: str
    title: str
    value: str
    aside: str = ""
    series: list[float | None] = field(default_factory=list)
    scale: tuple[float, float] | None = (0, 100)
    levels: tuple[float, float] | None = None
    pairs: list[tuple[str, str | Text]] = field(default_factory=list)
    severity: str | None = None
    footer: str = ""


def _pair_cell(label: str, content: str | Text, width: int) -> Text:
    content = content if isinstance(content, Text) else Text(content, style=TEXT)
    label_width = max(0, width - content.cell_len - 1)
    cell = Text(label[:label_width], style=LABEL)
    cell.append(" " * max(1, width - cell.cell_len - content.cell_len))
    cell.append_text(content)
    return cell


def pair_rows(pairs: list[tuple[str, str | Text]], width: int) -> list[Text]:
    """Label left, value right-aligned; two columns only when every pair fits."""
    gap = 4
    cell_width = (width - gap) // 2
    fits = all(len(label) + 2 + (content.cell_len if isinstance(content, Text) else len(content)) <= cell_width
               for label, content in pairs)
    columns = 2 if fits else 1
    cell_width = cell_width if fits else width
    rows = []
    for start in range(0, len(pairs), columns):
        row = Text()
        for index, (label, content) in enumerate(pairs[start:start + columns]):
            if index:
                row.append(" " * gap)
            row.append_text(_pair_cell(label, content, cell_width))
        rows.append(row)
    return rows


def packed_pairs(pairs: list[tuple[str, str | Text]], width: int, lines: int) -> list[Text]:
    """Compact cards: one pair per line if there is room, else pack them."""
    if lines <= 0:
        return []
    if len(pairs) <= lines:
        return [_pair_cell(label, content, width) for label, content in pairs]
    rows: list[Text] = []
    row = Text()
    for label, content in pairs:
        item = Text(f"{label} ", style=LABEL)
        item.append_text(content if isinstance(content, Text) else Text(content, style=TEXT))
        if row.cell_len and row.cell_len + 3 + item.cell_len > width:
            rows.append(row)
            row = Text()
        if row.cell_len:
            row.append(" · ", style=FAINT)
        row.append_text(item)
    rows.append(row)
    return rows[:lines]


def render_card(data: CardData, width: int, height: int, compact: bool, accent: str) -> Text:
    if width <= 0 or height <= 0:
        return Text()
    lines: list[Text] = []
    value = flagged(data.value, data.severity, style=f"bold {TEXT}")
    if compact:
        head = Text()
        head.append(f"{GLYPH} ", style=accent)
        head.append(data.title, style=f"bold {LABEL}")
        head.append("  ")
        head.append_text(value)
        room = width - head.cell_len - 2
        if room >= 8:
            trace = min(16, room)
            head.append(" " * (width - head.cell_len - trace))
            head.append(sparkline(data.series[-60:], trace), style=accent)
        lines.append(head)
        lines += packed_pairs(data.pairs, width, height - 1)
    else:
        head = value
        if data.aside:
            head.append(" " * max(2, width - head.cell_len - len(data.aside)))
            head.append(data.aside, style=LABEL)
        rows = pair_rows(data.pairs, width)
        graph_height = height - 1 - len(rows)
        while rows and graph_height < 3:
            rows.pop()
            graph_height += 1
        lines.append(head)
        if graph_height >= 8:
            lines.append(Text())
            graph_height -= 1
        lines += area_graph(data.series, width, graph_height, data.scale, accent, data.levels)
        lines += rows
    for line in lines:
        line.truncate(width, overflow="ellipsis")
    return Text("\n").join(lines[:height])


class Card(Widget):
    """One resource panel; its graph and rows are sized to the space it gets."""

    def __init__(self, key: str) -> None:
        super().__init__(id=f"card_{key}", classes="card")
        self.key = key
        self.data: CardData | None = None
        self.compact = False

    def show(self, data: CardData, compact: bool) -> None:
        self.data, self.compact = data, compact
        self.border_title = None if compact else f" [{ACCENT}]{GLYPH}[/] [b {TEXT}]{escape(data.title)}[/] "
        self.border_subtitle = None if compact or not data.footer else f"[{MUTED}] {escape(data.footer)} [/]"
        self.refresh()

    def render(self) -> Text:
        if self.data is None:
            return Text("collecting…", style=LABEL)
        return render_card(self.data, self.content_size.width, self.content_size.height,
                           self.compact, ACCENT)


def _devices(snapshot: Snapshot, name: str) -> str:
    metric = snapshot.metrics.get(name)
    match = re.search(r"\(([^)]*)\)$", metric.source) if metric else None
    return match.group(1) if match else ""


def _summed(history: History, *names: str) -> list[float | None]:
    series = [history.values(name) for name in names]
    length = min(len(values) for values in series)
    total = []
    for index in range(-length, 0):
        parts = [values[index] for values in series]
        total.append(None if any(part is None for part in parts) else sum(parts))
    return total


def reading(snapshot: Snapshot, name: str, unit: str, levels: tuple[float, float] | None,
            digits: int = 0) -> Text:
    """A card value, flagged when it crosses `levels`."""
    return flagged(f"{number(snapshot, name, digits)} {unit}", severity(value(snapshot, name), levels))


def overview_cards(snapshot: Snapshot, history: History) -> dict[str, CardData]:
    cards: dict[str, CardData] = {}
    s = snapshot

    cpu = value(s, "CPU usage")
    core_count = sum(re.fullmatch(r"CPU core \d+ usage", name) is not None for name in s.metrics)
    strip = Text()
    for index in range(core_count):
        core = value(s, f"CPU core {index} usage")
        strip.append("·" if core is None else SPARKS[min(7, int(core * 8 / 100))],
                     style=tone(core))
    cards["cpu"] = CardData(
        "cpu", "CPU", f"{number(s, 'CPU usage')} %", f"{number(s, 'CPU frequency', 2)} GHz",
        history.values("CPU usage"), pairs=[
            ("Load 1·5·15 min", f"{number(s, 'CPU load 1m', 2)}  {number(s, 'CPU load 5m', 2)}  {number(s, 'CPU load 15m', 2)}"),
            ("Temperature", reading(s, "CPU temperature", "°C", TEMP_LEVELS)),
            (f"{core_count} logical cores", strip),
            ("Pressure (some)", f"{number(s, 'CPU pressure some')} %"),
        ], severity=severity(cpu, USAGE_LEVELS), levels=USAGE_LEVELS)

    ram = value(s, "RAM usage")
    cards["memory"] = CardData(
        "memory", "MEMORY", f"{number(s, 'RAM usage', 0)} %",
        f"{number(s, 'RAM used')} / {number(s, 'RAM total')} GiB",
        history.values("RAM usage"), pairs=[
            ("Available", f"{number(s, 'RAM available')} GiB"),
            ("Swap", f"{number(s, 'Swap used')} / {number(s, 'Swap total')} GiB  ({number(s, 'Swap usage', 0)} %)"),
            ("Pressure (some)", f"{number(s, 'MEMORY pressure some')} %"),
            ("Pressure (full)", f"{number(s, 'MEMORY pressure full')} %"),
        ], severity=severity(ram, USAGE_LEVELS), levels=USAGE_LEVELS)

    gpus = gpu_cards(s)
    for index in range(2):
        key = f"gpu{index + 1}"
        if index >= len(gpus):
            cards[key] = CardData(key, f"GPU {index + 1}", "—", "not detected",
                                  pairs=[("Activity / VRAM / power", "unavailable")])
            continue
        name = gpus[index]
        prefix = f"GPU {name}"
        power_label, power = gpu_power(s, name)
        device = s.metrics.get(f"{prefix} device")
        slot = str(device.value).split(" · ")[-1] if device and device.value else ""
        cards[key] = CardData(
            key, gpu_title(s, name), f"{number(s, f'{prefix} activity', 0)} %",
            f"{number(s, f'{prefix} temperature', 0)} °C",
            history.values(f"{prefix} activity"), pairs=[
                ("VRAM", f"{number(s, f'{prefix} VRAM used', 0)} / {number(s, f'{prefix} VRAM total', 0)} MiB"),
                (power_label, f"{power} W"),
                ("Temperature", reading(s, f"{prefix} temperature", "°C", TEMP_LEVELS)),
                ("Device", f"{name} · {slot}" if slot else name),
            ])  # a busy GPU is working, not in trouble: no activity levels

    charge = value(s, "Battery charge")
    state = s.metrics.get("Battery state")
    state_text = str(state.value) if state and state.value else "State unavailable"
    uptime = value(s, "Uptime")
    cards["battery"] = CardData(
        "battery", "BATTERY", f"{number(s, 'Battery charge', 0)} %", state_text,
        history.values("Battery charge"), pairs=[
            ("Power", battery_power_label(s).removeprefix("Power ")),
            ("Uptime", duration(uptime) if uptime is not None else "—"),
        ], severity=severity(charge, BATTERY_LEVELS) if state_text == "Discharging" else None)

    fan, ec = fan_status(s)
    thermal_pairs: list[tuple[str, str | Text]] = [
        (f"GPU {index + 1}", reading(s, f"GPU {name} temperature", "°C", TEMP_LEVELS))
        for index, name in enumerate(gpus[:2])]
    thermal_pairs.append(("Fan", fan.replace("Fan speed ", "")))
    thermal_pairs += [(f"Fan {part.split(' ')[0]}", part.split(" ", 2)[-1])
                      for part in ec.split(" · ") if ec]
    cards["thermal"] = CardData(
        "thermal", "TEMPERATURES & FAN", f"CPU {number(s, 'CPU temperature', 0)} °C", fan,
        history.values("CPU temperature"), scale=(20, 100), levels=TEMP_LEVELS,
        pairs=thermal_pairs, severity=severity(value(s, "CPU temperature"), TEMP_LEVELS),
        footer="20–100 °C scale")

    disk_series = _summed(history, "Disk read", "Disk write")
    devices = _devices(s, "Disk read")
    cards["disk"] = CardData(
        "disk", f"DISK · {devices}" if devices else "DISK",
        human_rate(summed_value(s, "Disk read", "Disk write")),
        "read + write", disk_series, scale=None, pairs=[
            ("Read", human_rate(value(s, "Disk read"))),
            ("Write", human_rate(value(s, "Disk write"))),
            ("/ free", flagged(f"{number(s, 'Root disk free')} of {number(s, 'Root disk total', 0)} GiB",
                               severity(value(s, "Root disk usage"), DISK_LEVELS))),
            ("I/O pressure", f"{number(s, 'IO pressure some')} %"),
        ], footer=f"peak {human_rate(max(filter(None, disk_series), default=0))}")

    net_series = _summed(history, "Network receive", "Network send")
    devices = _devices(s, "Network receive")
    cards["net"] = CardData(
        "net", f"NETWORK · {devices}" if devices else "NETWORK",
        human_rate(summed_value(s, "Network receive", "Network send")),
        "down + up", net_series, scale=None, pairs=[
            ("Receive", human_rate(value(s, "Network receive"))),
            ("Send", human_rate(value(s, "Network send"))),
        ], footer=f"peak {human_rate(max(filter(None, net_series), default=0))}")
    return cards


def watch_alerts(snapshot: Snapshot, history: History) -> list[tuple[str, str]]:
    """What needs attention, worst first, as (level, text) pairs.

    CPU load counts over the last 10 s, so a one-second spike is not an alert.
    A busy GPU is not one either; hot or full things are.
    """
    s = snapshot
    recent = [load for load in history.values("CPU usage", 10) if load is not None]
    checks: list[tuple[str, float | None, tuple[float, float], str]] = [
        ("CPU", sum(recent) / len(recent) if recent else value(s, "CPU usage"), USAGE_LEVELS, "%"),
        ("RAM", value(s, "RAM usage"), USAGE_LEVELS, "%"),
        ("CPU", value(s, "CPU temperature"), TEMP_LEVELS, "°C"),
    ]
    checks += [(f"GPU {index + 1}", value(s, f"GPU {name} temperature"), TEMP_LEVELS, "°C")
               for index, name in enumerate(gpu_cards(s))]
    checks.append(("Disk", value(s, "Root disk usage"), DISK_LEVELS, "% full"))
    state = s.metrics.get("Battery state")
    if state is not None and state.value == "Discharging":
        checks.append(("Battery", value(s, "Battery charge"), BATTERY_LEVELS, "%"))
    alerts = [(level, f"{label} {current:.0f} {unit}") for label, current, levels, unit in checks
              if current is not None and (level := severity(current, levels))]
    return sorted(alerts, key=lambda alert: alert[0] != "crit")


def terminal_size() -> tuple[int, int] | None:
    """Size of whichever standard stream, or /dev/tty, is the terminal.

    Textual asks stdout, and falls back to 80 × 24 when stdout is a pipe (for
    example when a shell captures the command's output), which would draw the
    dashboard in one corner of a much larger terminal.
    """
    for stream in (sys.__stdout__, sys.__stderr__, sys.__stdin__):
        try:
            size = os.get_terminal_size(stream.fileno())
        except (AttributeError, OSError, ValueError):
            continue
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:
        return None
    try:
        size = os.get_terminal_size(fd)
        return (size.columns, size.lines) if size.columns > 0 and size.lines > 0 else None
    except OSError:
        return None
    finally:
        os.close(fd)


def with_terminal_size(driver_class: type[Driver]) -> type[Driver]:
    if not hasattr(driver_class, "_get_terminal_size") or issubclass(driver_class, HeadlessDriver):
        return driver_class

    class TerminalSizeDriver(driver_class):  # type: ignore[misc, valid-type]
        def _get_terminal_size(self) -> tuple[int, int]:
            return terminal_size() or super()._get_terminal_size()

    return TerminalSizeDriver


class MonitorApp(App):
    TITLE = "sysvigil"
    BINDINGS = [
        ("1", "show_tab('overview')", "Overview"),
        ("2", "show_tab('cores')", "Cores"),
        ("3", "show_tab('sensors')", "Sensors"),
        ("4", "show_tab('pressure')", "Pressure"),
        ("5", "show_tab('sources')", "Sources"),
        ("c", "sort_processes('cpu')", "Sort CPU"),
        ("m", "sort_processes('memory')", "Sort RAM"),
        ("i", "sort_processes('io')", "Sort I/O"),
        ("p", "sort_processes('pid')", "Sort PID"),
        ("r", "reverse_processes", "Reverse"),
        ("q", "quit", "Quit"),
    ]
    CSS = f"""
    Screen {{ layout: vertical; background: {PAGE}; color: {TEXT}; }}
    Footer {{ background: {SURFACE}; }}
    #stamp {{ height: 1; padding: 0 1; background: {PAGE}; }}
    #cards {{ layout: grid; grid-size: 4 2; grid-rows: 1fr; grid-gutter: 0 1; padding: 0 1; }}
    .card {{ height: 100%; width: 100%; background: {SURFACE};
             border: round {BORDER}; border-title-align: left;
             border-subtitle-align: right; padding: 0 1; }}
    #throughput {{ height: 2; padding: 0 1; display: none; }}
    #views {{ height: 1fr; }}
    TabPane {{ height: 1fr; padding: 0; }}
    #sort_info {{ height: 1; padding: 0 1; color: {LABEL}; }}
    #process_table {{ height: 1fr; background: {PAGE}; }}
    #process_table > .datatable--header {{ background: {SURFACE}; color: {LABEL}; text-style: bold; }}
    #process_table > .datatable--even-row {{ background: {mix(SURFACE, PAGE, 0.5)}; }}
    #process_table > .datatable--odd-row {{ background: {PAGE}; }}
    #process_table > .datatable--cursor {{ background: {mix(ACCENT, PAGE, 0.3)}; }}
    .details {{ height: 1fr; overflow: auto; padding: 1 2; }}
    Screen.compact #cards {{ grid-size: 2 3; grid-gutter: 0 2; }}
    Screen.compact .card {{ border: none; padding: 0 1; }}
    Screen.compact #throughput {{ display: block; }}
    Screen.compact #card_disk, Screen.compact #card_net {{ display: none; }}
    Screen.short Footer {{ display: none; }}
    """

    def __init__(self, collector: Collector | None = None) -> None:
        super().__init__()
        # Tabs, footer keys, and scrollbars follow the same palette.
        self.register_theme(Theme(
            name="sysvigil", primary=ACCENT, accent=ACCENT, foreground=TEXT, background=PAGE,
            surface=SURFACE, panel=SURFACE, success=GOOD, warning=WARN, error=CRIT, dark=True))
        self.theme = "sysvigil"
        self.collector = collector or Collector()
        self.history = History(HISTORY_SECONDS)
        self.latest: Snapshot | None = None
        self.sort_field = "cpu"
        self.descending = True
        self._table_layout: tuple[int, bool] | None = None
        self._primed = False
        self._sampling = False
        self._sample_error: str | None = None
        self._beat = True
        uname = os.uname()
        self._host = f"{uname.nodename} · Linux {uname.release.split('-')[0]}"

    def get_driver_class(self) -> type[Driver]:
        return with_terminal_size(super().get_driver_class())

    def compose(self) -> ComposeResult:
        yield Static(id="stamp")
        with Grid(id="cards"):
            for key in CARD_KEYS:
                yield Card(key)
        yield Static(id="throughput")
        with TabbedContent(id="views"):
            with TabPane("1 Processes", id="overview"):
                yield Static(id="sort_info")
                yield DataTable(id="process_table", zebra_stripes=True)
            with TabPane("2 Cores", id="cores"):
                yield Static(id="cores_details", classes="details")
            with TabPane("3 Sensors", id="sensors"):
                yield Static(id="sensors_details", classes="details")
            with TabPane("4 Pressure", id="pressure"):
                yield Static(id="pressure_details", classes="details")
            with TabPane("5 Sources", id="sources"):
                yield Static(id="sources_details", classes="details")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#process_table", DataTable)
        table.cursor_type = "row"
        self.configure_process_columns()
        self.update_layout()
        self.render_stamp()
        self.sample_in_background()
        self.set_interval(1.0, self.sample_in_background)

    def on_resize(self) -> None:
        # App._on_resize stores the new size after this handler returns, so
        # lay out once it has; self.size is still the old size here.
        self.call_later(self.apply_terminal_size)

    def apply_terminal_size(self) -> None:
        self.update_layout()
        # One resize arrives before on_mount, which sets the table up itself.
        if self.screen.is_mounted:
            self.configure_process_columns()
            self.refresh_processes()

    @property
    def wide_table(self) -> bool:
        return self.size.width >= WIDE_MIN_WIDTH

    def configure_process_columns(self) -> None:
        width = self.size.width
        if (width, self.wide_table) == self._table_layout:
            return
        self._table_layout = (width, self.wide_table)
        table = self.query_one("#process_table", DataTable)
        table.clear(columns=True)
        # Each column adds two cells of padding; two more for the scrollbar.
        if self.wide_table:
            columns = WIDE_COLUMNS
            widths = [8, 22, 12, 8, 10, 12]
        else:
            columns = COMPACT_COLUMNS
            widths = [7, 0, 8, 10, 11]
        rest = width - sum(widths) - 2 * len(columns) - 2
        if self.wide_table:
            widths.append(max(20, rest))
        else:
            widths[1] = max(16, rest)
        for (label, key), column_width in zip(columns, widths):
            table.add_column(label, width=column_width, key=key)

    def update_layout(self) -> None:
        width, height = self.size
        budget = (height - 2) * 0.55
        wide = width >= WIDE_MIN_WIDTH and budget / 2 >= CARD_MIN_HEIGHT
        if not wide and width >= 80 and budget / 4 >= CARD_MIN_HEIGHT:
            wide = True  # a tall, narrower terminal: 2 × 4 bordered cards
        columns = 4 if width >= WIDE_MIN_WIDTH else 2
        cards = self.query_one("#cards", Grid)
        self.screen.set_class(not wide, "compact")
        self.screen.set_class(height < 30, "short")
        if wide:
            rows = len(CARD_KEYS) // columns
            cards.styles.grid_size_columns = columns
            cards.styles.grid_size_rows = rows
            cards.styles.height = int(budget // rows) * rows
        else:
            cards.styles.grid_size_columns = 2
            cards.styles.grid_size_rows = 3
            cards.styles.height = 3 * (3 if height < 30 else max(3, min(5, int(budget // 3))))
        if self.latest is not None:
            self.render_overview()

    def action_show_tab(self, tab: str) -> None:
        self.query_one("#views", TabbedContent).active = tab

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_details()

    def action_sort_processes(self, field: str) -> None:
        if self.sort_field == field:
            self.descending = not self.descending
        else:
            self.sort_field, self.descending = field, True
        self.refresh_processes()

    def action_reverse_processes(self) -> None:
        self.descending = not self.descending
        self.refresh_processes()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        key = event.column_key.value
        if key in ("cpu", "memory", "io", "pid"):
            self.action_sort_processes(key)

    def sample_in_background(self) -> None:
        """Skip a tick rather than queue samples if /proc is slow to walk."""
        if not self._sampling:
            self._sampling = True
            self._sample_worker()

    @work(thread=True, exit_on_error=False)
    def _sample_worker(self) -> None:
        try:
            if not self._primed:
                self.collector.prime()
                self._primed = True
                return
            snapshot = self.collector.sample()
        except Exception as error:
            # Keep running and retry next tick, but say the data is stale.
            self.call_from_thread(self.report_sample_error, error)
            return
        finally:
            self._sampling = False
        self.call_from_thread(self.apply_snapshot, snapshot)

    def refresh_data(self) -> None:
        """Sample on the calling thread; the timer uses sample_in_background."""
        self.apply_snapshot(self.collector.sample())

    def report_sample_error(self, error: Exception) -> None:
        self._sample_error = " ".join(f"{type(error).__name__}: {error}".split())
        self.render_stamp()

    def apply_snapshot(self, snapshot: Snapshot) -> None:
        self._sample_error = None
        self._beat = not self._beat
        self.latest = snapshot
        self.history.add(snapshot)
        self.render_overview()
        self.refresh_details()
        self.refresh_processes()

    def refresh_details(self) -> None:
        """Only the visible detail tab is rebuilt; others render when opened."""
        if self.latest is None:
            return
        group = self.query_one("#views", TabbedContent).active
        if group not in ("cores", "sensors", "pressure", "sources"):
            return
        metrics = display_metrics(self.latest)
        names = metric_names(self.latest, "all" if group == "sources" else group, metrics)
        self.query_one(f"#{group}_details", Static).update(
            metric_table(self.latest, names, group == "sources", metrics))

    def render_stamp(self) -> None:
        """Eye (it blinks with each sample), host, what needs attention, clock."""
        name = Text.assemble(("◉" if self._beat else "◎", f"bold {ACCENT}"), " ", ("sysvigil", f"bold {TEXT}"))
        uptime = value(self.latest, "Uptime") if self.latest else None
        host = Text(f"   {self._host}" + (f" · up {duration(uptime)}" if uptime is not None else ""),
                    style=MUTED)
        watch = Text()
        if self.latest is not None:
            alerts = watch_alerts(self.latest, self.history)
            for level, text in alerts:
                if watch.cell_len:
                    watch.append("  ")
                watch.append_text(flagged(text, level))
            if not alerts:
                watch.append("● all quiet", style=GOOD)
        if self._sample_error is not None:
            clock = short = Text(f"sampling failed · {self._sample_error}", style=CRIT)
        elif self.latest is None:
            clock = short = Text("collecting first sample…", style=MUTED)
        else:
            now = datetime.fromtimestamp(self.latest.timestamp).astimezone()
            clock = Text(f"1 s refresh   {now:%H:%M:%S %Z}", style=MUTED)
            short = Text(f"{now:%H:%M:%S}", style=MUTED)
        # Narrow terminals drop the refresh note, then the host, before alerts.
        room = self.size.width - 2
        for host_part, clock_part in ((host, clock), (host, short), (Text(), short)):
            if name.cell_len + host_part.cell_len + watch.cell_len + clock_part.cell_len + 6 <= room:
                break
        clock_part.truncate(max(1, room - name.cell_len - 3), overflow="ellipsis")
        watch.truncate(max(0, room - name.cell_len - host_part.cell_len - clock_part.cell_len - 6),
                       overflow="ellipsis")
        right = Text.assemble(watch, "   " if watch.cell_len else "", clock_part)
        stamp = Text.assemble(name, host_part)
        stamp.append(" " * max(1, room - stamp.cell_len - right.cell_len))
        stamp.append_text(right)
        self.query_one("#stamp", Static).update(stamp)

    def render_overview(self) -> None:
        snapshot = self.latest
        if snapshot is None:
            return
        self.render_stamp()
        compact = self.screen.has_class("compact")
        for key, data in overview_cards(snapshot, self.history).items():
            self.query_one(f"#card_{key}", Card).show(data, compact)
        if compact:
            read, write = human_rate(value(snapshot, "Disk read")), human_rate(value(snapshot, "Disk write"))
            receive, send = human_rate(value(snapshot, "Network receive")), human_rate(value(snapshot, "Network send"))
            io = Text()
            io.append(f"{GLYPH} ", style=ACCENT)
            io.append("DISK ", style=f"bold {LABEL}")
            io.append(f"↓{read} ↑{write}   ", style=TEXT)
            io.append(f"{GLYPH} ", style=ACCENT)
            io.append("NET ", style=f"bold {LABEL}")
            io.append(f"↓{receive} ↑{send}\n", style=TEXT)
            io.append("  / free ", style=MUTED)
            io.append_text(flagged(f"{number(snapshot, 'Root disk free')} GiB",
                                   severity(value(snapshot, "Root disk usage"), DISK_LEVELS)))
            io.append("   disk ", style=MUTED)
            io.append(sparkline(_summed(self.history, "Disk read", "Disk write")[-60:], 12), style=ACCENT)
            io.append("  net ", style=MUTED)
            io.append(sparkline(_summed(self.history, "Network receive", "Network send")[-60:], 12), style=ACCENT)
            self.query_one("#throughput", Static).update(io)

    def process_cells(self, row: ProcessRow) -> dict[str, str | Text]:
        cpu = Text(f"{row.cpu_percent:.1f}" if row.cpu_percent is not None else "—", justify="right",
                   style=tone(row.cpu_percent, normal=TEXT) if row.cpu_percent is not None else FAINT)
        return {
            "pid": Text(str(row.pid), style=MUTED),
            "name": Text(row.name, style=f"bold {TEXT}"),
            "user": Text(row.user, style=LABEL),
            "cpu": cpu,
            "memory": Text(human_mib(row.memory_mib), justify="right"),
            "io": Text(human_rate(row.io_mib_s) if row.io_mib_s else "—" if row.io_mib_s is None else "0",
                       justify="right", style=TEXT if row.io_mib_s else FAINT),
            "command": Text(row.command, style=MUTED) if row.command else Text(f"[{row.name}]", style=FAINT),
        }

    def refresh_processes(self) -> None:
        if self.latest is None:
            return
        direction = "↓" if self.descending else "↑"
        info = Text()
        info.append(f"{len(self.latest.processes)} processes", style=f"bold {TEXT}")
        info.append(f"   sorted by {self.sort_field.upper()} {direction}", style=LABEL)
        info.append("   c CPU · m RAM · i I/O · p PID · r reverse", style=MUTED)
        self.query_one("#sort_info", Static).update(info)
        table = self.query_one("#process_table", DataTable)
        columns = [key.value for key in table.columns]
        rows = sorted_processes(self.latest.processes, self.sort_field, self.descending)[:PROCESS_ROWS]
        # Rows are positional and updated in place, so the cursor and scroll
        # position survive each refresh instead of jumping back to the top.
        for index, row in enumerate(rows):
            values = self.process_cells(row)
            cells = [values[column] for column in columns]
            key = f"row{index}"
            if index >= table.row_count:
                table.add_row(*cells, key=key)
                continue
            for column, cell in zip(columns, cells):
                if table.get_cell(key, column) != cell:
                    table.update_cell(key, column, cell)
        while table.row_count > len(rows):
            table.remove_row(f"row{table.row_count - 1}")
