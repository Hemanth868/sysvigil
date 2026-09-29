from pathlib import Path
import subprocess
import sys

from sysvigil.cli import format_snapshot
from sysvigil.collector import Metric, ProcessRow, Snapshot


def test_snapshot_text_lists_values_sources_and_top_processes() -> None:
    data = Snapshot(0, {
        "CPU usage": Metric(12.5, "%", "/proc/stat"),
        "Uptime": Metric(3720.0, "s", "/proc/uptime"),
        "Fan amdgpu fan1": Metric(None, "RPM", "/sys/fan"),
    }, [ProcessRow(7, "python", 3.0, 44.0, None)])
    text = format_snapshot(data)
    assert "  CPU usage: 12.50 % [source: /proc/stat]" in text
    assert "  Uptime: 1 h 2 min [source: /proc/uptime]" in text
    assert "  Fan speed: Fan speed unavailable [source: /sys/fan]" in text
    assert "  7 | python | 3.0 % | 44.0 MiB | unavailable" in text


def test_once_does_not_import_textual() -> None:
    code = "import sys, sysvigil.cli; assert 'textual' not in sys.modules, 'textual imported'"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parents[1])
