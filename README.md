# Minisforum N5 Air fan control

A small Linux service that runs the SSD and CPU fans of a Minisforum N5 Air from the drives' and the CPU's own temperatures, and sets those fans to full speed whenever a temperature cannot be trusted or the service stops.

The N5 Air's firmware runs its fans for a hard-drive NAS. That works for spinning disks, but not for a machine that reads NVMe drives hard: under sustained reads, the firmware kept the SSD fan near 2,200 rpm while a Samsung 990 PRO in a motherboard M.2 slot reached 80°C in under 6 minutes. The SSD fan appears to follow an embedded-controller board sensor, not the drives. This service reads the NVMe and CPU temperatures itself and sets the fans to match.

> **Tested on one machine.** Minisforum N5 Air, board `F8NAB`, Ubuntu 26.04, kernel 7.0, with the community driver at commit `feb6d9a`. The installer refuses any other board. The N5 and N5 Pro may wire their fans differently. This software writes to your embedded controller's fan registers; use it at your own risk.

## Results

Fifteen minutes of raw sequential reads on four NVMe drives at once (about 17.9GB/s in total; the 990 PRO alone about 7.1GB/s). The test stopped early if any drive reached 80°C.

| SSD fan control | SSD fan speed | 990 PRO (motherboard slot) |
| --- | --- | --- |
| Firmware, automatic | 2,250 to 2,300 rpm | 45 to 80°C, stopped at 5 min 54 s |
| BIOS "User" setting, PWM 1 = 100 | 2,720 to 2,770 rpm | 51 to 80°C, stopped at 5 min 50 s |
| Linux, fixed 100% | 4,740 to 4,800 rpm | 46 to 79°C, completed |
| This service | 2,490 to 4,790 rpm | 40 to 71°C, completed; 63 to 71°C from minute 7 |

Room and starting temperatures differed between runs, so treat the comparison as approximate. At idle the service holds the SSD fan at about 2,480 rpm and leaves the CPU fan off.

## What it controls

The [minisforum-n5-it5571](https://github.com/ltdstudio/minisforum-n5-it5571) driver exposes the N5's embedded controller (ITE IT5571) as a hwmon device. On the N5 Air:

| Channel | Fan | Speed sensor | This service |
| --- | --- | --- | --- |
| `pwm1` | CPU fan | none | CPU curve |
| `pwm2` | SSD fan, above the motherboard M.2 slots | `fan2_input` | SSD curve |
| `pwm3` | hard-drive fans | `fan3_input` | not touched; firmware control |
| `pwm4` | labeled "PCIe fan" by the driver | none | CPU curve |

The CPU fan has no speed sensor, and a listening test could not tell whether `pwm1` or `pwm4` drives it, so both follow the CPU curve.

| Fan | Input | Curve (input to duty) |
| --- | --- | --- |
| SSD | smallest margin, in °C, between any internal NVMe sensor and its drive's warning temperature (see below) | 30 or more to 35%, 20 to 60%, 12 to 85%, 7 or less to 100% |
| CPU | `k10temp` Tctl | off below 55°C; 55 to 25%, 70 to 55%, 80 to 80%, 88 to 100%; off again at 50°C or below |

- Linear between points and clamped at the ends, updated every 2 seconds.
- Speeds rise at once and fall by at most 4% per update.
- The CPU fan has no minimum speed: it is loud even at 25%, so it stays off until the CPU needs it. When it starts from off, it runs at 60% for one update so it spins up reliably.
- SSD fan speed by duty on our unit: 100% about 4,850 rpm, 70% about 3,930 rpm, 40% about 2,790 rpm.

To change a curve, edit `FANS` in `n5-fan-control.py` and run the installer again.

### How the SSD fan decides

Most NVMe drives report several temperatures: the composite (`temp1`), which the drive uses for its own warnings and throttling, and internal sensors (`temp2` and up, "Sensor 1" and "Sensor 2" on Samsung drives), which on our drives run 2 to 10°C above the composite. Each drive also reports its own warning temperature (`temp1_max`, the drive's WCTEMP: 82°C for our 990 PRO, 84 for the 9100 PRO, 85 for the 990 EVO, 83 for the SN730) and critical temperature (`temp1_crit`).

Every 2 seconds the service computes, for every sensor of every internal drive, the margin between the sensor and the drive's warning temperature, and sets the SSD fan from the smallest margin. The warning temperature is `temp1_max`, capped at `temp1_crit`; a drive that reports no `temp1_max` (the NVMe specification allows a WCTEMP of 0) is held to 70°C. Linux creates `temp1_max` only when WCTEMP is nonzero, `temp1_crit` only when CCTEMP is nonzero, and the sensor files only for sensors the drive reports, once, when it registers the drive (`nvme_hwmon_is_visible` in `drivers/nvme/host/hwmon.c`), so a file that disappears later is a fault.

While a sensor is rising, the margin is taken from where it is heading rather than where it is:

- The service keeps each sensor's last 5 readings and fits a straight line to them (least squares).
- It subtracts two standard errors from the line's slope, so a noisy run of readings counts only as far as it supports a rise. Without this, Sensor 1 of our idle 990 EVOs, which jumps up to 5°C between readings, looked like a rise of up to 12.7°C over the next 30 seconds; with it, at most 3.4°C.
- If the slope is still positive, the sensor counts as its latest reading plus that slope over the next 30 seconds. A falling trend is never projected: the latest reading counts, and the fan slows at the normal 4% per update.

The 30 seconds come from our measurements: when sustained reads start, the 990 PRO's composite climbs at up to 0.5°C per second, and a faster SSD fan shows on the drive 10 to 15 seconds later. Replaying the composite readings of four 15-minute stress runs, the prediction reached 85% duty 1.5 to 7 minutes earlier than the readings alone. A short burst of I/O can also lift the fan for about half a minute: once in about 12 minutes of observation at idle, a 990 PRO sensor rose 7°C in 4 seconds and the prediction asked for 83%.

The service logs every SSD fan change of 10% or more with the margin and the sensor behind it, for example `ssd fan 35 -> 83% (margin 13 C to warning, nvme2 temp3)`.

## Fail-safe

All three controlled channels go to full speed when:

- a temperature is missing, unreadable, or outside 5 to 120°C, including any internal sensor of an NVMe drive;
- a drive's `temp1_max` or `temp1_crit` exists but cannot be read or is outside 50 to 100°C;
- an internal NVMe drive has no temperature sensor, or an internal drive the service has seen before is missing, or one of its temperature or limit files (`temp*_input`, `temp1_max`, `temp1_crit`) is, including after a restart or a reboot. The list of drives and their files is kept in `/var/lib/n5-fan-control/nvme-drives`, one line per drive, by serial number: the kernel can give the drives different names (`nvme0`, `nvme1`) at every boot, so a new name is not a missing drive. Full speed also holds while that list cannot be written, and while an internal drive reports no serial number or the same one as another drive. A file counts once a complete listing of the drives has seen it, so a drive that first appears without `temp1_max` gets the 70°C default. A missing drive or file holds full speed for 5 minutes (see below);
- the service stops, crashes, or stops responding for 20 seconds (systemd watchdog; `ExecStopPost` runs `n5-fan-control --failsafe`).

The service also refuses to start unless its three channels are writable. After a sensor problem clears, speeds fall from full at the normal 4% per update, and trends start again from new readings. Optional parts do not affect fan control: an unreadable SSD fan speed is left out of the metrics, and a failed metrics write is logged.

A drive that overheats can stop responding and come back once it has cooled, so a missing drive or file first holds the fans at full speed. If the same drives and files are still missing after 5 minutes, the service treats it as a change of hardware (a drive removed, moved to a USB4 enclosure, or replaced; a replacement has a new serial number) rather than an overheated drive. It records the internal drives present instead, logs which drives or files it dropped, and the fans follow the drives present again. Any further change in what is missing starts the 5 minutes again. A list that cannot tell the drives apart, such as the list by kernel name that earlier versions kept, is replaced the same way after 5 minutes at full speed.

To skip the 5 minutes after changing drives on purpose, clear the list:

```bash
sudo rm /var/lib/n5-fan-control/nvme-drives
sudo systemctl restart n5-fan-control
```

### NVMe drives in USB4 enclosures

An NVMe drive in a USB4 or Thunderbolt enclosure appears as an ordinary NVMe controller, tunnelled over PCIe through the USB4 host router. The service ignores these drives: it does not record them, and their temperatures do not set the SSD fan, because they sit outside the case and are plugged and unplugged at will (a replug can also give the drive a new name, such as `nvme5` becoming `nvme6`). Internal drives keep the full fail-safe.

A drive counts as USB4-attached when a PCIe port on its path has a device-link supplier bound to the `thunderbolt` driver. The kernel creates that link from each USB4 PCIe tunnel port to its USB4 host interface. To check how the service will see your drives, look for `supplier:` links on the ports above each controller:

```bash
readlink -f /sys/class/nvme/nvme*/device
ls -d /sys/bus/pci/devices/*/supplier:*
```

Any drive without such a link counts as internal, so a missed detection keeps the fail-safe. If a recorded name now belongs to a USB4 drive (an internal drive dropped out and the enclosure took its name), the fans go to full speed.

If a version without this check recorded a USB4 drive in the list, remove that name from the list and restart the service.

On this board, setting a channel back to automatic (`pwm_enable=2`) returns the SSD fan to the firmware but does **not** release the CPU fan channels. Once Linux has set them, they stay where Linux left them until a reboot. That is why every exit path leaves them at full speed.

## Driver patch

The upstream driver's `experimental_write=1` makes every fan channel writable. `driver/writable-mask.patch` adds a `writable_mask` module parameter: a channel whose bit is clear gets no `pwm` nodes, and the driver rejects writes to it. The installer sets `writable_mask=0xb` (`pwm1`, `pwm2`, `pwm4`), so the hard-drive fans stay under firmware control.

## Install

Requirements: Python 3 (standard library only), DKMS, kernel headers, and `git`.

1. Build the patched driver with DKMS (this repository cloned next to the driver's):

   ```bash
   git clone https://github.com/ltdstudio/minisforum-n5-it5571.git
   git -C minisforum-n5-it5571 checkout feb6d9a5fbf6662a52fa7496009537945d2b30b0
   sudo mkdir /usr/src/minisforum_n5_it5571-0.2.0
   sudo cp minisforum-n5-it5571/driver-prototype/{minisforum_n5_it5571.c,Makefile} /usr/src/minisforum_n5_it5571-0.2.0/
   sudo cp minisforum-n5-fan-control/driver/dkms.conf /usr/src/minisforum_n5_it5571-0.2.0/
   sudo patch -d /usr/src/minisforum_n5_it5571-0.2.0 -p1 < minisforum-n5-fan-control/driver/writable-mask.patch
   sudo dkms install minisforum_n5_it5571/0.2.0
   ```

   Because the patch is applied in the DKMS source tree, kernel updates rebuild the patched module.

2. Install and start the controller:

   ```bash
   cd minisforum-n5-fan-control
   sudo ./install.sh
   ```

   The installer stops at the first failed step. It exits successfully only when the service is running, `pwm1`, `pwm2` and `pwm4` exist, and `pwm3` does not.

3. Check it:

   ```bash
   journalctl -u n5-fan-control -f
   ```

## Configuration

`/etc/default/n5-fan-control` (installed from `n5-fan-control.default` if absent) has one optional setting:

- `N5_FAN_PROM_DIR`: a directory for a Prometheus textfile, `n5_fan.prom`. It holds `n5_fan_duty_percent`, `n5_fan_rpm`, `n5_fan_control_temp_celsius` (`source="nvme_max"`, the hottest composite, and `source="cpu"`), `n5_fan_ssd_margin_celsius` (the margin that sets the SSD fan) and, per drive, `n5_fan_nvme_hottest_celsius` (hottest sensor now), `n5_fan_nvme_predicted_celsius` (hottest planned temperature, including the prediction), `n5_fan_nvme_warning_celsius` and `n5_fan_nvme_margin_celsius`. Empty by default, which means no metrics. Point it at node_exporter's textfile directory (or one that Grafana Alloy reads) if you use one. The unit allows writes to `/var/lib/prometheus/node-exporter`; any other directory needs a `ReadWritePaths=` drop-in.

Restart the service after changing it.

## BIOS settings

The BIOS fan settings apply only while Linux is not controlling a channel: during boot, before the service starts, or if the driver fails to load. We set the SSD fan to **User** with PWM 1 = 100 (about 2,730 rpm, a little more than automatic) as the fallback, and left the CPU and hard-drive fans on **System**.

## Uninstall

```bash
sudo systemctl disable --now n5-fan-control
sudo rm /usr/local/sbin/n5-fan-control /etc/systemd/system/n5-fan-control.service \
  /etc/default/n5-fan-control /etc/modprobe.d/n5-fan-control.conf /etc/modules-load.d/n5-fan-control.conf
sudo rm -r /var/lib/n5-fan-control
sudo systemctl daemon-reload
sudo reboot
```

Stopping the service leaves the controlled fans at full speed. The reboot returns them to the firmware.

## Tests

```bash
python3 tests/test_n5_fan_control.py
```

The tests run the controller against a fake sysfs tree and the installer's device lookup against a fake hwmon tree. They need no hardware and write nothing outside a temporary directory.

## License

GPL-2.0-only, the same as the driver it patches. See `LICENSE`. The driver itself is by its upstream authors at [ltdstudio/minisforum-n5-it5571](https://github.com/ltdstudio/minisforum-n5-it5571). This project is not affiliated with Minisforum.
