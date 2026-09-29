from pathlib import Path

from collections import namedtuple

from sysvigil.collector import (
    battery_metrics, battery_power_w, core_usages, device_rates, gpu_metrics,
    hardware_devices, percentage, pressure, rate, sensor_metrics,
    valid_ec_percent, valid_rpm,
)


def put(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def test_fan_sensor_rejects_bravo_sentinel_and_accepts_valid_rpm(tmp_path: Path) -> None:
    hw = tmp_path / "class/hwmon/hwmon4"
    put(hw / "name", "amdgpu\n")
    put(hw / "fan1_input", "65535\n")
    put(hw / "fan1_max", "3350\n")
    assert not valid_rpm(65535, 3350)
    assert sensor_metrics(tmp_path)["Fan amdgpu fan1"].value is None
    put(hw / "fan1_input", "1800\n")
    assert sensor_metrics(tmp_path)["Fan amdgpu fan1"].value == 1800
    assert valid_rpm(0, 3350)
    assert not valid_rpm(3351, 3350)


def test_optional_ec_is_percent_and_missing_sensors_are_unavailable(tmp_path: Path) -> None:
    missing = sensor_metrics(tmp_path)
    assert missing["CPU temperature"].value is None
    assert missing["GPU temperature"].value is None
    assert missing["Fan speed"].value is None
    assert missing["MSI EC CPU fan"].value is None
    assert missing["MSI EC CPU fan"].unit == "%"
    put(tmp_path / "devices/platform/msi-ec/cpu/realtime_fan_speed", "72\n")
    put(tmp_path / "devices/platform/msi-ec/gpu/realtime_fan_speed", "151\n")
    sensors = sensor_metrics(tmp_path)
    assert sensors["MSI EC CPU fan"].value == 72
    assert sensors["MSI EC GPU fan"].value is None
    assert valid_ec_percent(150)
    assert not valid_ec_percent(151)


def test_rates_handle_elapsed_time_and_reset() -> None:
    assert rate(100, 310, 2) == 105
    assert rate(310, 100, 2) is None
    assert rate(100, 310, 0) is None
    assert rate(None, 310, 2) is None


def test_missing_pressure_file_and_some_only(tmp_path: Path) -> None:
    assert pressure("cpu", tmp_path)["some"].value is None
    put(tmp_path / "pressure/cpu", "some avg10=3.25 avg60=1.00 avg300=0.50 total=1\n")
    result = pressure("cpu", tmp_path)
    assert result["some"].value == 3.25
    assert result["full"].value is None


def test_per_core_usage_uses_each_core_delta() -> None:
    Times = namedtuple("Times", "user system idle iowait guest guest_nice")
    old = [Times(0, 0, 0, 0, 0, 0), Times(0, 0, 0, 0, 0, 0)]
    new = [Times(1, 0, 1, 0, 0, 0), Times(2, 0, 0, 0, 0, 0)]
    assert core_usages(old, new) == [50.0, 100.0]
    assert core_usages(None, new) == [None, None]


def test_amd_drm_activity_vram_and_missing_files(tmp_path: Path) -> None:
    missing = gpu_metrics(tmp_path)
    assert missing["GPU activity"].value is None
    assert missing["GPU VRAM used"].value is None
    device = tmp_path / "class/drm/card1/device"
    put(device / "gpu_busy_percent", "37\n")
    put(device / "mem_info_vram_used", str(256 * 2**20))
    put(device / "mem_info_vram_total", str(512 * 2**20))
    metrics = gpu_metrics(tmp_path)
    assert metrics["GPU card1 activity"].value == 37
    assert metrics["GPU card1 VRAM used"].value == 256
    assert metrics["GPU card1 VRAM total"].value == 512
    put(device / "gpu_busy_percent", "101")
    put(device / "mem_info_vram_used", str(513 * 2**20))
    invalid = gpu_metrics(tmp_path)
    assert invalid["GPU card1 activity"].value is None
    assert invalid["GPU card1 VRAM used"].value is None


def test_gpu_temperature_matches_drm_card(tmp_path: Path) -> None:
    pci = tmp_path / "pci-gpu"
    pci.mkdir()
    card = tmp_path / "class/drm/card2"
    card.mkdir(parents=True)
    (card / "device").symlink_to(pci, target_is_directory=True)
    hw = tmp_path / "class/hwmon/hwmon0"
    put(hw / "name", "amdgpu")
    put(hw / "temp1_input", "52000")
    (hw / "device").symlink_to(pci, target_is_directory=True)
    assert sensor_metrics(tmp_path)["GPU card2 temperature"].value == 52


def test_percentages_and_battery_power_direction() -> None:
    assert percentage(25, 100) == 25
    assert percentage(101, 100) is None
    assert percentage(0, 0) is None
    assert battery_power_w("Charging", 12_000_000, None, None) == 12
    assert battery_power_w("Discharging", None, 2_000_000, 12_000_000) == -24
    assert battery_power_w("Not charging", None, 0, 12_000_000) == 0
    assert battery_power_w("Not charging", 4_000_000, None, None) is None
    assert battery_power_w("Unknown", 4_000_000, None, None) is None
    # Drivers that sign their readings: direction still comes from status.
    assert battery_power_w("Discharging", None, -1_500_000, 12_000_000) == -18
    assert battery_power_w("Discharging", -9_000_000, None, None) == -9
    assert battery_power_w("Charging", None, 1_500_000, 12_000_000) == 18
    assert battery_power_w("Discharging", None, 1_500_000, -12_000_000) is None


def test_battery_power_fallback_and_missing_sensor(tmp_path: Path) -> None:
    assert battery_metrics(tmp_path)["Battery power"].value is None
    bat = tmp_path / "class/power_supply/BAT1"
    put(bat / "capacity", "55")
    put(bat / "status", "Discharging")
    put(bat / "current_now", "1500000")
    put(bat / "voltage_now", "12000000")
    result = battery_metrics(tmp_path)
    assert result["Battery power"].value == -18
    assert "current_now" in result["Battery power"].source
    put(bat / "power_now", "9000000")
    assert battery_metrics(tmp_path)["Battery power"].value == -9


def test_gpu_power_uses_labelled_hwmon_and_invalid_is_unavailable(tmp_path: Path) -> None:
    pci = tmp_path / "pci-gpu"
    pci.mkdir()
    card = tmp_path / "class/drm/card1"
    card.mkdir(parents=True)
    (card / "device").symlink_to(pci, target_is_directory=True)
    hw = tmp_path / "class/hwmon/hwmon4"
    put(hw / "name", "amdgpu")
    put(hw / "power1_average", "11000000")
    put(hw / "power1_label", "PPT")
    (hw / "device").symlink_to(pci, target_is_directory=True)
    assert gpu_metrics(tmp_path)["GPU card1 PPT power"].value == 11
    put(hw / "power1_average", "999999999")
    assert gpu_metrics(tmp_path)["GPU card1 PPT power"].value is None


def test_only_hardware_block_and_network_devices_are_counted(tmp_path: Path) -> None:
    for name in ("nvme0n1", "sda"):
        put(tmp_path / "block" / name / "device" / "vendor", "x")
    for name in ("zram0", "loop0", "dm-0"):
        (tmp_path / "block" / name).mkdir(parents=True)
    assert hardware_devices(tmp_path / "block") == {"nvme0n1", "sda"}
    assert hardware_devices(tmp_path / "missing") == set()


def test_device_rates_ignore_new_devices_and_isolate_resets() -> None:
    old = {"nvme0n1": (1000, 2000), "sda": (500, 500)}
    new = {"nvme0n1": (3000, 2000), "sda": (100, 700), "sdb": (10**9, 10**9)}
    # sdb appeared between samples; sda's read counter reset.
    assert device_rates(old, new, 2) == (1000.0, 100.0)
    assert device_rates(None, new, 1) is None
    assert device_rates(old, {}, 1) is None
    assert device_rates(old, {"sdb": (1, 1)}, 1) is None
