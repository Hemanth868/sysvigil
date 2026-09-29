import asyncio
import time

from textual.widgets import DataTable, Static, TabbedContent

from sysvigil.collector import Metric, ProcessRow, Snapshot
from sysvigil.ui import (
    Card, History, MonitorApp, area_graph, bar, display_metrics, fan_status,
    human_rate, metric_names, overview_cards, pair_rows, sorted_processes, sparkline,
)


def snapshot(timestamp: float | None = None) -> Snapshot:
    metrics = {
        "CPU usage": Metric(12.5, "%", "/proc/stat"),
        "CPU core 0 usage": Metric(20.0, "%", "/proc/stat (cpu0)"),
        "CPU frequency": Metric(3.5, "GHz", "/sys/cpufreq"),
        "CPU load 1m": Metric(1.2, "runnable tasks", "/proc/loadavg"),
        "CPU load 5m": Metric(1.0, "runnable tasks", "/proc/loadavg"),
        "CPU load 15m": Metric(0.8, "runnable tasks", "/proc/loadavg"),
        "RAM used": Metric(8.5, "GiB", "/proc/meminfo"),
        "RAM total": Metric(16.0, "GiB", "/proc/meminfo"),
        "RAM available": Metric(7.5, "GiB", "/proc/meminfo"),
        "RAM usage": Metric(53.0, "%", "/proc/meminfo"),
        "Swap used": Metric(1.0, "GiB", "/proc/meminfo"),
        "Swap total": Metric(8.0, "GiB", "/proc/meminfo"),
        "Swap usage": Metric(12.5, "%", "/proc/meminfo"),
        "CPU temperature": Metric(59.0, "°C", "/sys/temperature"),
        "GPU card1 device": Metric("Navi 14 / RX 5500 family · 0000:03:00.0", "device", "/sys/gpu1"),
        "GPU card1 temperature": Metric(50.0, "°C", "/sys/gpu1_temp"),
        "GPU card1 activity": Metric(35, "%", "/sys/activity"),
        "GPU card1 VRAM used": Metric(256.0, "MiB", "/sys/vram"),
        "GPU card1 VRAM total": Metric(4080.0, "MiB", "/sys/vram_total"),
        "GPU card1 PPT power": Metric(11.0, "W", "/sys/power"),
        "GPU card2 device": Metric("Renoir / Radeon Vega · 0000:07:00.0", "device", "/sys/gpu2"),
        "GPU card2 temperature": Metric(53.0, "°C", "/sys/gpu2_temp"),
        "GPU card2 activity": Metric(7, "%", "/sys/activity2"),
        "GPU card2 VRAM used": Metric(442.0, "MiB", "/sys/vram2"),
        "GPU card2 VRAM total": Metric(512.0, "MiB", "/sys/vram_total2"),
        "GPU card2 PPT power": Metric(13.0, "W", "/sys/power2"),
        "Battery charge": Metric(79, "%", "/sys/battery"),
        "Battery state": Metric("Not charging", "state", "/sys/battery_status"),
        "Battery power": Metric(0.0, "W", "/sys/current_now × /sys/voltage_now"),
        "Fan amdgpu fan1": Metric(None, "RPM", "/sys/fan"),
        "MSI EC CPU fan": Metric(None, "%", "/sys/msi_cpu"),
        "MSI EC GPU fan": Metric(None, "%", "/sys/msi_gpu"),
        "Root disk free": Metric(18.0, "GiB", "statvfs(/)"),
        "Disk read": Metric(1.0, "MiB/s", "/proc/diskstats"),
        "Disk write": Metric(0.5, "MiB/s", "/proc/diskstats"),
        "Network receive": Metric(0.1, "MiB/s", "/proc/net/dev"),
        "Network send": Metric(0.2, "MiB/s", "/proc/net/dev"),
        "CPU pressure some": Metric(1.0, "% of last 10 s", "/proc/pressure/cpu"),
    }
    rows = [ProcessRow(index + 1, f"process-{index}", 12.0 - index, 20.0 + index * 10,
                       None if index == 0 else float(index)) for index in range(12)]
    return Snapshot(timestamp if timestamp is not None else time.time(), metrics, rows)


class FakeCollector:
    def prime(self) -> None:
        pass

    def sample(self) -> Snapshot:
        return snapshot()


def test_history_is_60_seconds_and_sparkline_has_fixed_width() -> None:
    history = History()
    history.add(snapshot(100))
    history.add(snapshot(130))
    history.add(snapshot(161))
    assert len(history.values("CPU usage")) == 2
    assert len(sparkline([1, 2, 3], 8)) == 8
    assert sparkline([None], 3).endswith("·")
    assert len(sparkline(list(range(60)), 20)) == 20
    assert bar(0, 8) == "╍" * 8
    assert bar(100, 8) == "━" * 8
    assert bar(None, 8) == "·" * 8


def test_area_graph_fills_from_the_bottom_and_marks_missing_samples() -> None:
    lines = [line.plain for line in area_graph([None, 0, 50, 100], 5, 2, (0, 100), "white")]
    # Padding and the missing sample show a floor; zero is still visible.
    assert lines == ["    █", "▁▁▁██"]
    assert area_graph([5, 10], 4, 3, None, "white")[0].plain.endswith("█")  # auto scale peaks at max


def test_readable_units_and_pairs_never_cut_labels() -> None:
    assert human_rate(None) == "—"
    assert human_rate(0) == "0 B/s"
    assert human_rate(0.5) == "512.0 KiB/s"
    assert human_rate(3.25) == "3.2 MiB/s"
    rows = pair_rows([("Swap", "2.1 / 8.0 GiB  (26 %)"), ("Available", "5.7 GiB")], 50)
    assert len(rows) == 2 and rows[0].plain.startswith("Swap")
    rows = pair_rows([("Read", "0 B/s"), ("Write", "1.0 KiB/s")], 50)
    assert len(rows) == 1 and "Write" in rows[0].plain


def test_missing_fans_collapse_and_percent_is_separate() -> None:
    data = snapshot()
    shown = display_metrics(data)
    assert "Fan speed" in shown
    assert len([name for name in shown if name.startswith(("Fan ", "MSI EC "))]) == 1
    assert fan_status(data)[0] == "Fan speed unavailable"
    assert "65535" not in str(shown)
    data.metrics["MSI EC CPU fan"] = Metric(72, "%", "/sys/msi_cpu")
    assert display_metrics(data)["MSI EC CPU fan"].unit == "%"
    assert "72 %" in fan_status(data)[1]


def test_unavailable_disk_and_network_totals_are_not_shown_as_zero() -> None:
    data = snapshot()
    data.metrics["Disk read"] = Metric(None, "MiB/s", "/proc/diskstats (no hardware devices)")
    data.metrics["Network send"] = Metric(None, "MiB/s", "/proc/net/dev (no hardware devices)")
    history = History()
    history.add(data)
    cards = overview_cards(data, history)
    assert cards["disk"].value == "—"
    assert cards["net"].value == "—"
    assert overview_cards(snapshot(), history)["disk"].value == "1.5 MiB/s"


def test_sorting_uses_all_processes_and_keeps_missing_last() -> None:
    rows = snapshot().processes
    assert sorted_processes(rows, "memory", True)[0].pid == 12
    assert sorted_processes(rows, "memory", False)[0].pid == 1
    assert sorted_processes(rows, "io", True)[-1].pid == 1


def test_details_keep_sources_off_overview() -> None:
    data = snapshot()
    assert "CPU core 0 usage" in metric_names(data, "cores")
    assert "GPU card1 PPT power" in metric_names(data, "sensors")
    assert "CPU pressure some" in metric_names(data, "pressure")


def test_layout_at_80x24_and_160x45() -> None:
    async def check(size: tuple[int, int]) -> None:
        app = MonitorApp(FakeCollector())
        async with app.run_test(size=size) as pilot:
            app.refresh_data()
            await pilot.pause()
            compact = size[0] == 80
            cards = app.query_one("#cards")
            cpu = app.query_one("#card_cpu", Card)
            gpu = app.query_one("#card_gpu1", Card)
            io = app.query_one("#throughput", Static)
            table = app.query_one("#process_table", DataTable)
            tabs = app.query_one("#views", TabbedContent)
            assert cards.region.width == size[0]
            assert app.screen.has_class("compact") == compact
            # Wide terminals give the cards over half the height, with graphs.
            assert cpu.region.height == (3 if compact else 11)
            assert gpu.region.width >= (38 if compact else 36)
            assert table.region.height >= (8 if compact else 12)
            assert table.row_count == 12
            assert len(table.columns) == (5 if compact else 7)
            assert "VRAM" in str(gpu.render())
            assert "/sys/" not in str(cpu.render())
            assert "unavailable" in str(app.query_one("#card_thermal", Card).render())
            if compact:
                assert "DISK" in str(io.render()) and "NET" in str(io.render())
                assert not app.query_one("#card_disk").display
            else:
                assert not io.display
                assert "Read" in str(app.query_one("#card_disk", Card).render())
                assert "█" in str(cpu.render()) or "▁" in str(cpu.render())
            table.move_cursor(row=5, animate=False)
            app.refresh_data()
            await pilot.pause()
            assert table.cursor_row == 5
            assert table.row_count == 12
            await pilot.press("m")
            assert app.sort_field == "memory"
            assert table.get_cell_at((0, 0)).plain == "12"
            await pilot.press("3")
            await pilot.pause()
            assert tabs.active == "sensors"
            assert "GPU card1 PPT power" in app.query_one("#sensors_details", Static).content.columns[0]._cells
            await pilot.press("4")
            assert tabs.active == "pressure"
            await pilot.press("5")
            assert tabs.active == "sources"
            await pilot.press("1")
            assert tabs.active == "overview"

    asyncio.run(check((80, 24)))
    asyncio.run(check((160, 45)))


def test_driver_takes_size_from_the_terminal_even_when_stdout_is_piped(monkeypatch) -> None:
    from textual.drivers.headless_driver import HeadlessDriver
    from textual.drivers.linux_driver import LinuxDriver
    import sysvigil.ui as ui

    driver = ui.with_terminal_size(LinuxDriver)
    assert issubclass(driver, LinuxDriver) and driver is not LinuxDriver
    assert ui.with_terminal_size(HeadlessDriver) is HeadlessDriver
    monkeypatch.setattr(ui, "terminal_size", lambda: (238, 50))
    assert driver._get_terminal_size(object.__new__(driver)) == (238, 50)
