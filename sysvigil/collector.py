"""Read-only Linux measurements, independent of presentation and Textual."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import re
import time

import psutil


SYS = Path("/sys")
PROC = Path("/proc")


@dataclass(frozen=True)
class Metric:
    value: float | str | None
    unit: str
    source: str


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    name: str
    cpu_percent: float | None
    memory_mib: float
    io_mib_s: float | None
    user: str = ""
    command: str = ""


@dataclass(frozen=True)
class Snapshot:
    timestamp: float
    metrics: dict[str, Metric]
    processes: list[ProcessRow]


def rate(previous: int | float | None, current: int | float | None, seconds: float) -> float | None:
    """A counter reset is missing data, rather than a negative rate."""
    if previous is None or current is None or seconds <= 0 or current < previous:
        return None
    return (current - previous) / seconds


def device_rates(previous: dict[str, tuple[int, ...]] | None, current: dict[str, tuple[int, ...]] | None,
                 seconds: float) -> tuple[float | None, ...] | None:
    """Sum per-device rates over devices present in both samples.

    Hot-plugged or removed devices then cannot appear as a jump in a summed
    counter, and one device's counter reset only drops that device.
    """
    if not previous or not current or seconds <= 0:
        return None
    shared = [name for name in current if name in previous]
    if not shared:
        return None
    width = len(next(iter(current.values())))
    totals: list[float | None] = []
    for index in range(width):
        rates = [rate(previous[name][index], current[name][index], seconds) for name in shared]
        valid = [value for value in rates if value is not None]
        totals.append(sum(valid) if valid else None)
    return tuple(totals)


def hardware_devices(directory: Path) -> set[str]:
    """Names under /sys/block or /sys/class/net backed by real hardware.

    Only physical devices have a `device` link, so loop, zram (swap in RAM),
    device-mapper, loopback, bridge, and veth traffic is not counted twice.
    """
    try:
        return {path.name for path in directory.iterdir() if (path / "device").exists()}
    except OSError:
        return set()


def valid_rpm(value: int | None, maximum: int | None = None) -> bool:
    if value is None or value < 0 or value == 65535:
        return False
    if maximum is not None and maximum > 0:
        return value <= maximum
    return value <= 20000


def valid_ec_percent(value: int | None) -> bool:
    return value is not None and 0 <= value <= 150


def percentage(part: int | float | None, total: int | float | None) -> float | None:
    if part is None or total is None or total <= 0 or part < 0 or part > total:
        return None
    return part * 100 / total


def battery_power_w(status: str | None, power_uw: int | None,
                    current_ua: int | None, voltage_uv: int | None) -> float | None:
    """Positive is charging; negative is discharging; zero means idle."""
    if power_uw is not None and power_uw >= 0:
        watts = power_uw / 1_000_000
    elif current_ua is not None and voltage_uv is not None and current_ua >= 0 and voltage_uv >= 0:
        watts = current_ua * voltage_uv / 1_000_000_000_000
    else:
        return None
    if status == "Charging":
        return watts
    if status == "Discharging":
        return -watts
    if status in ("Full", "Not charging") and watts == 0:
        return 0.0
    return None


def read_number(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def pressure(resource: str, proc_root: Path = PROC) -> dict[str, Metric]:
    path = proc_root / "pressure" / resource
    result: dict[str, Metric] = {}
    content = read_text(path)
    for kind in ("some", "full"):
        match = re.search(rf"(?m)^{kind}\s+avg10=([0-9.]+)", content or "")
        result[kind] = Metric(float(match.group(1)) if match else None, "% of last 10 s", str(path))
    return result


def sensor_metrics(sys_root: Path = SYS) -> dict[str, Metric]:
    result: dict[str, Metric] = {}
    hwmon = sys_root / "class" / "hwmon"
    cards = {str((card / "device").resolve()): card.name
             for card in gpu_card_dirs(sys_root)}
    cpu_found = False
    gpu_index = 0
    fan_found = False
    for directory in sorted(hwmon.glob("hwmon*")):
        name = read_text(directory / "name")
        if name == "k10temp" and not cpu_found:
            path = directory / "temp1_input"
            raw = read_number(path)
            result["CPU temperature"] = Metric(raw / 1000 if raw is not None and -20000 < raw < 125000 else None, "°C", str(path))
            cpu_found = True
        if name == "amdgpu":
            gpu_index += 1
            path = directory / "temp1_input"
            raw = read_number(path)
            card_name = cards.get(str((directory / "device").resolve()))
            label = f"GPU {card_name}" if card_name else f"GPU {gpu_index}"
            result[f"{label} temperature"] = Metric(raw / 1000 if raw is not None and -20000 < raw < 125000 else None, "°C", str(path))
        for path in sorted(directory.glob("fan*_input")):
            fan_found = True
            maximum = read_number(path.with_name(path.name.replace("_input", "_max")))
            raw = read_number(path)
            key = f"Fan {name or directory.name} {path.stem.replace('_input', '')}"
            result[key] = Metric(raw if valid_rpm(raw, maximum) else None, "RPM", str(path))
    if not cpu_found:
        result["CPU temperature"] = Metric(None, "°C", str(hwmon / "*/name=k10temp,temp1_input"))
    if not gpu_index:
        result["GPU temperature"] = Metric(None, "°C", str(hwmon / "*/name=amdgpu,temp1_input"))
    if not fan_found:
        result["Fan speed"] = Metric(None, "RPM", str(hwmon / "*/fan*_input"))

    ec = sys_root / "devices" / "platform" / "msi-ec"
    for device in ("cpu", "gpu"):
        path = ec / device / "realtime_fan_speed"
        raw = read_number(path)
        result[f"MSI EC {device.upper()} fan"] = Metric(raw if valid_ec_percent(raw) else None, "%", str(path))
    return result


def gpu_card_dirs(sys_root: Path = SYS) -> list[Path]:
    drm = sys_root / "class" / "drm"
    return sorted((path for path in drm.glob("card*") if re.fullmatch(r"card\d+", path.name)),
                  key=lambda path: int(path.name[4:]))


def gpu_metrics(sys_root: Path = SYS) -> dict[str, Metric]:
    """Read AMD DRM activity, VRAM, and labelled hwmon power when published."""
    result: dict[str, Metric] = {}
    cards = gpu_card_dirs(sys_root)
    hwmon_by_device = {
        str((hw / "device").resolve()): hw
        for hw in (sys_root / "class" / "hwmon").glob("hwmon*")
        if read_text(hw / "name") == "amdgpu"
    }
    for card in cards:
        device = card / "device"
        prefix = f"GPU {card.name}"
        vendor = read_text(device / "vendor")
        device_id = read_text(device / "device")
        slot = device.resolve().name
        known = {"0x7340": "Navi 14 / RX 5500 family", "0x1636": "Renoir / Radeon Vega"}
        identity = known.get(device_id or "", "AMD GPU" if vendor == "0x1002" else "GPU")
        result[f"{prefix} device"] = Metric(f"{identity} · {slot}", "device", str(device / "device"))
        activity_path = device / "gpu_busy_percent"
        busy = read_number(activity_path)
        result[f"{prefix} activity"] = Metric(busy if busy is not None and 0 <= busy <= 100 else None,
                                               "%", str(activity_path))
        total_path = device / "mem_info_vram_total"
        used_path = device / "mem_info_vram_used"
        total = read_number(total_path)
        used = read_number(used_path)
        valid_total = total is not None and total > 0
        result[f"{prefix} VRAM used"] = Metric(used / 2**20 if used is not None and used >= 0 and (not valid_total or used <= total) else None,
                                                "MiB", str(used_path))
        result[f"{prefix} VRAM total"] = Metric(total / 2**20 if valid_total else None, "MiB", str(total_path))
        hw = hwmon_by_device.get(str(device.resolve()))
        power_path = None
        if hw is not None:
            power_path = next(iter(sorted(hw.glob("power*_average"))), None)
            if power_path is None:
                power_path = next(iter(sorted(hw.glob("power*_input"))), None)
        if power_path is None:
            result[f"{prefix} reported power"] = Metric(None, "W", str(device / "hwmon/*/power*_average|input"))
            continue
        raw_power = read_number(power_path)
        label = read_text(power_path.with_name(re.sub(r"_(average|input)$", "_label", power_path.name)))
        power_name = f"{prefix} {label or 'reported'} power"
        result[power_name] = Metric(raw_power / 1_000_000 if raw_power is not None and 0 <= raw_power <= 500_000_000 else None,
                                    "W", str(power_path))
    if not cards:
        result["GPU activity"] = Metric(None, "%", str(sys_root / "class/drm/card*/device/gpu_busy_percent"))
        result["GPU VRAM used"] = Metric(None, "MiB", str(sys_root / "class/drm/card*/device/mem_info_vram_used"))
        result["GPU VRAM total"] = Metric(None, "MiB", str(sys_root / "class/drm/card*/device/mem_info_vram_total"))
        result["GPU reported power"] = Metric(None, "W", str(sys_root / "class/drm/card*/device/hwmon/*/power*_average|input"))
    return result


def battery_metrics(sys_root: Path = SYS) -> dict[str, Metric]:
    batteries = sorted((sys_root / "class" / "power_supply").glob("BAT*"))
    if not batteries:
        return {"Battery charge": Metric(None, "%", str(sys_root / "class/power_supply/BAT*/capacity")),
                "Battery state": Metric(None, "state", str(sys_root / "class/power_supply/BAT*/status")),
                "Battery power": Metric(None, "W", str(sys_root / "class/power_supply/BAT*/power_now"))}
    battery = batteries[0]
    capacity = read_number(battery / "capacity")
    status = read_text(battery / "status")
    power_now = read_number(battery / "power_now")
    current_now = read_number(battery / "current_now")
    voltage_now = read_number(battery / "voltage_now")
    power_source = str(battery / "power_now") if power_now is not None else f"{battery / 'current_now'} × {battery / 'voltage_now'}"
    return {
        "Battery charge": Metric(capacity if capacity is not None and 0 <= capacity <= 100 else None, "%", str(battery / "capacity")),
        "Battery state": Metric(status, "state", str(battery / "status")),
        "Battery power": Metric(battery_power_w(status, power_now, current_now, voltage_now), "W", power_source),
    }


def cpu_usage(previous: object, current: object) -> float | None:
    try:
        old = previous._asdict()
        new = current._asdict()
        fields = set(old) & set(new) - {"guest", "guest_nice"}
        total = sum(new[field] - old[field] for field in fields)
        idle = sum(new[field] - old[field] for field in ("idle", "iowait") if field in fields)
        return max(0.0, min(100.0, (total - idle) * 100 / total)) if total > 0 else None
    except (AttributeError, ZeroDivisionError):
        return None


def core_usages(previous: list | None, current: list) -> list[float | None]:
    return [cpu_usage(previous[index], times) if previous is not None and index < len(previous) else None
            for index, times in enumerate(current)]


class Collector:
    def __init__(self, sys_root: Path = SYS, proc_root: Path = PROC):
        self.sys_root = sys_root
        self.proc_root = proc_root
        self.previous: dict | None = None
        # A process's command line and owner rarely change, so read them once
        # per (pid, start time) instead of every second.
        self._identity: dict[tuple[int, float], tuple[str, str]] = {}
        self._users: dict[int, str] = {}

    def _user(self, uid: int) -> str:
        if uid not in self._users:
            try:
                self._users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._users[uid] = str(uid)
        return self._users[uid]

    def _process_identity(self, proc: psutil.Process, key: tuple[int, float]) -> tuple[str, str]:
        if key not in self._identity:
            try:
                user = self._user(proc.uids().real)
            except (psutil.AccessDenied, OSError):
                user = ""
            try:
                command = " ".join(proc.cmdline())
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                command = ""
            self._identity[key] = (user, command)
        return self._identity[key]

    def _processes(self) -> dict[tuple[int, float], tuple[str, float, int, int | None, str, str]]:
        rows = {}
        for proc in psutil.process_iter():
            try:
                with proc.oneshot():
                    key = (proc.pid, proc.create_time())
                    times = proc.cpu_times()
                    cpu_time = times.user + times.system
                    memory = proc.memory_info().rss
                    try:
                        io = proc.io_counters()
                        io_bytes = io.read_bytes + io.write_bytes
                    except (psutil.AccessDenied, OSError):
                        io_bytes = None
                    rows[key] = (proc.name(), cpu_time, memory, io_bytes) + self._process_identity(proc, key)
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, OSError):
                continue
        self._identity = {key: value for key, value in self._identity.items() if key in rows}
        return rows

    def _counters(self) -> dict:
        disks = hardware_devices(self.sys_root / "block")
        nics = hardware_devices(self.sys_root / "class" / "net")
        try:
            disk = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        except (OSError, RuntimeError):
            disk = {}
        try:
            net = psutil.net_io_counters(pernic=True, nowrap=False) or {}
        except OSError:
            net = {}
        return {
            "time": time.monotonic(), "cpu": psutil.cpu_times(),
            "cpu_cores": psutil.cpu_times(percpu=True),
            "disk": {name: (c.read_bytes, c.write_bytes) for name, c in disk.items() if name in disks},
            "net": {name: (c.bytes_recv, c.bytes_sent) for name, c in net.items() if name in nics},
            "processes": self._processes(),
        }

    def prime(self) -> None:
        self.previous = self._counters()

    def sample(self) -> Snapshot:
        current = self._counters()
        previous = self.previous
        self.previous = current
        elapsed = current["time"] - previous["time"] if previous else 0
        metrics: dict[str, Metric] = {}
        uptime = read_text(self.proc_root / "uptime")
        try:
            seconds = float(uptime.split()[0]) if uptime else None
        except ValueError:
            seconds = None
        metrics["Uptime"] = Metric(seconds, "s", str(self.proc_root / "uptime"))
        metrics["CPU usage"] = Metric(cpu_usage(previous["cpu"], current["cpu"]) if previous else None, "%", "/proc/stat")
        for index, usage in enumerate(core_usages(previous["cpu_cores"] if previous else None,
                                                 current["cpu_cores"])):
            metrics[f"CPU core {index} usage"] = Metric(usage, "%", f"/proc/stat (cpu{index})")
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        metrics["RAM used"] = Metric(vm.used / 2**30, "GiB", "/proc/meminfo")
        metrics["RAM total"] = Metric(vm.total / 2**30, "GiB", "/proc/meminfo")
        metrics["RAM available"] = Metric(vm.available / 2**30, "GiB", "/proc/meminfo (MemAvailable)")
        metrics["RAM usage"] = Metric(vm.percent, "%", "/proc/meminfo (MemAvailable/total)")
        metrics["Swap used"] = Metric(swap.used / 2**30, "GiB", "/proc/meminfo")
        metrics["Swap total"] = Metric(swap.total / 2**30, "GiB", "/proc/meminfo")
        metrics["Swap usage"] = Metric(percentage(swap.used, swap.total), "%", "/proc/meminfo (SwapTotal/SwapFree)")
        frequency = psutil.cpu_freq()
        metrics["CPU frequency"] = Metric(frequency.current / 1000 if frequency and frequency.current > 0 else None,
                                           "GHz", "psutil.cpu_freq() (/sys/devices/system/cpu/cpu*/cpufreq)")
        try:
            loads = os.getloadavg()
        except OSError:
            loads = (None, None, None)
        for period, load in zip(("1m", "5m", "15m"), loads):
            metrics[f"CPU load {period}"] = Metric(load, "runnable tasks", "/proc/loadavg")
        try:
            usage = psutil.disk_usage("/")
            metrics["Root disk used"] = Metric(usage.used / 2**30, "GiB", "statvfs(/)")
            metrics["Root disk total"] = Metric(usage.total / 2**30, "GiB", "statvfs(/)")
            metrics["Root disk free"] = Metric(usage.free / 2**30, "GiB", "statvfs(/)")
            metrics["Root disk usage"] = Metric(usage.percent, "%", "statvfs(/)")
        except OSError:
            metrics["Root disk used"] = Metric(None, "GiB", "statvfs(/)")
            metrics["Root disk total"] = Metric(None, "GiB", "statvfs(/)")
            metrics["Root disk free"] = Metric(None, "GiB", "statvfs(/)")
            metrics["Root disk usage"] = Metric(None, "%", "statvfs(/)")
        for kind, source in (("disk", "/proc/diskstats"), ("net", "/proc/net/dev")):
            rates = device_rates(previous[kind] if previous else None, current[kind], elapsed)
            devices = ", ".join(sorted(current[kind])) or "no hardware devices"
            labels = ("Disk read", "Disk write") if kind == "disk" else ("Network receive", "Network send")
            for index, label in enumerate(labels):
                bytes_per_second = rates[index] if rates else None
                value = bytes_per_second / 2**20 if bytes_per_second is not None else None
                metrics[label] = Metric(value, "MiB/s", f"{source} ({devices})")
        metrics.update(sensor_metrics(self.sys_root))
        metrics.update(gpu_metrics(self.sys_root))
        metrics.update(battery_metrics(self.sys_root))
        for resource in ("cpu", "memory", "io"):
            for kind, metric in pressure(resource, self.proc_root).items():
                metrics[f"{resource.upper()} pressure {kind}"] = metric
        processes = []
        for (pid, created), (name, cpu_time, memory, io_bytes, user, command) in current["processes"].items():
            old = previous["processes"].get((pid, created)) if previous else None
            cpu_rate = rate(old[1], cpu_time, elapsed) if old else None
            io_rate = rate(old[3], io_bytes, elapsed) if old else None
            cpu = cpu_rate * 100 if cpu_rate is not None else None
            io = io_rate / 2**20 if io_rate is not None else None
            processes.append(ProcessRow(pid, name, cpu, memory / 2**20, io, user, command))
        processes.sort(key=lambda row: (row.cpu_percent or 0, row.memory_mib), reverse=True)
        return Snapshot(time.time(), metrics, processes)
