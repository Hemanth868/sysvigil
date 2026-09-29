# sysvigil

Lightweight Linux resource monitor – live CPU, memory, disk, network and process stats in the terminal.

sysvigil shows a full-screen [Textual](https://textual.textualize.io/) dashboard or prints a one-second text snapshot. It is read-only: it reads `/proc`, `/sys`, and filesystem statistics through `psutil`, and never writes to hardware: no fan control, no EC writes, and no kernel modules.

It was built on an MSI Bravo 15 (Ryzen with Renoir iGPU, Radeon RX 5500M) running Fedora, and works on other Linux machines; hardware it can't find shows as unavailable.

## Install and run

```sh
python -m pip install .
sysvigil            # dashboard
sysvigil --once     # one-second snapshot, printed as text
```

From a checkout without installing, run `python3 -m sysvigil`. Textual can live in `.vendor/` (the local test setup), and `psutil` comes from the system Python (`sudo dnf install python3-psutil` on Fedora).

## Using the dashboard

| Key | Action |
| --- | --- |
| `1` – `5` | Processes, per-core CPU and memory, sensors, pressure, measurement sources |
| `c` `m` `i` `p` | Sort processes by CPU, RAM, I/O, or PID; press again to reverse |
| `r` | Reverse the sort |
| `q` | Quit |

The top half of the screen holds cards for CPU, memory, both GPUs, battery, temperatures and fans, disk, and network; the bottom half holds the process table.

The layout follows the terminal size. At 140 columns and about 32 rows or more, eight bordered cards sit in a 4 × 2 grid with filled graphs, and the process table adds User and Command columns. A tall terminal narrower than that uses a 2 × 4 grid. Smaller terminals, down to 80 × 24, use six compact cards with one-line sparklines and a disk/network strip. The terminal size is read from whichever of stdout, stderr, stdin, or `/dev/tty` is a terminal, so the dashboard still fills the window when a shell or tool captures the command's stdout (Textual alone would assume 80 × 24).

Card graphs draw one column per one-second sample, newest on the right, so wider cards show more history (up to five minutes); compact sparklines cover the last 60 seconds. Percentage graphs use a fixed 0–100 % scale, and their rows turn amber above 75 % and red above 90 %. The temperature graph uses a fixed 20–100 °C scale, and disk and network graphs scale from zero to the peak shown in the card's lower border. Values turn amber or red only when utilization is high or battery charge is low. Rates switch units automatically (B/s, KiB/s, MiB/s).

## How measurements work

Rates compare counters one second apart, per device, over devices present in both samples, so a hot-plugged device or one device's counter reset never shows up as a spike. Disk rates cover hardware block devices only (entries in `/sys/block` with a `device` link, such as `nvme0n1`); loop devices, zram swap, and device-mapper layers are left out, since zram traffic is RAM and dm traffic would be counted twice. Network rates likewise cover hardware interfaces only, leaving out loopback, bridges, and veth pairs. The Sources view lists the devices counted. CPU pressure and memory/I/O pressure show Linux PSI `some` and `full` 10-second averages. Process CPU percentages use one logical CPU as 100%, so a multithreaded process can exceed 100%.
The process list includes only processes visible in the current `/proc` namespace; a container or sandbox may show fewer processes than the host desktop.

The AMD `fan*_input` readings are reported as RPM only when valid: negative readings, the 65535 sentinel, and values above a sensor's `fan*_max` are rejected. If no maximum is published, the safety ceiling is 20,000 RPM. Zero RPM is a valid stopped-fan reading. The optional `msi-ec` provider reads `/sys/devices/platform/msi-ec/{cpu,gpu}/realtime_fan_speed` only if present and labels its values `%`, never RPM. Missing fan providers collapse to one “Fan speed unavailable” message. No kernel module installation or EC writes are performed.

For the Bravo 15 A4DDR, the upstream [msi-ec model configuration](https://github.com/BeardOverflow/msi-ec/blob/main/msi-ec.c) lists EC firmware `16WKEMS1.105` and CPU/GPU real-time fan addresses `0x71`/`0x89`. Its [documented sysfs interface](https://github.com/BeardOverflow/msi-ec/blob/main/README.md) defines both real-time fan values as 0–100 or 0–150 percent. The provider does not read raw EC addresses; it only reads the driver's published sysfs files if they already exist. The laptop's BIOS version does not establish its EC firmware version.

Each measurement includes a unit and source; long paths are in the Sources view. Unavailable values indicate missing, inaccessible, or invalid readings. Top process I/O may be unavailable when `/proc/PID/io` is inaccessible. The overview shows up to 40 visible processes, sorted from the full process set, and updates rows in place so the cursor and scroll position hold across refreshes; the snapshot command prints the top eight. Sampling runs on a background thread, so a slow `/proc` walk never freezes the dashboard, and only the visible detail tab is rebuilt each second.

The AMD GPU [activity counter](https://docs.kernel.org/gpu/amdgpu/thermal.html) is a percentage, and the [VRAM counters](https://docs.kernel.org/gpu/amdgpu/driver-misc.html) are bytes converted to MiB. The laptop's GPU power sensors are labelled `PPT` by their AMD hwmon driver, so the dashboard uses that label rather than calling them GPU die power. Battery power uses `power_now` when available, otherwise `current_now × voltage_now`; positive watts mean charging, negative watts mean discharging, and zero watts on this laptop means idle. Missing files stay unavailable.

## Tests

```sh
python -m pytest -q
```

The tests use fake `/sys` trees and a fake collector, so they run on any machine.
