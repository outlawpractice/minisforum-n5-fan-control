#!/usr/bin/env python3
"""Minisforum N5 Air fan controller: drives the SSD and CPU fans from Linux.

The firmware's fan curves suit a hard-drive NAS: under sustained NVMe reads the SSD
fan stayed near 2,200 rpm while a Samsung 990 PRO in a motherboard M.2 slot reached
80 C. This service sets the SSD fan from how close any internal NVMe drive is to its
own warning temperature, counting every sensor the drive reports and, while a sensor
is rising, where it is heading (drives in USB4 enclosures are ignored), and the CPU
fan from the CPU temperature, through the community minisforum_n5_it5571 driver with
the writable_mask patch in driver/ (write access only to the channels named below).
See README.md for measurements, installation and configuration.

Fail-safe: a missing or implausible temperature or warning temperature, a drive or
sensor that drops out, a failed write, a stop or a crash all leave the controlled
fans at full speed. The CPU fan
channel does not return to firmware control on this board, so full speed is also
the state after the service ends. `--failsafe` sets full speed and exits; the unit
runs it on every stop.

Install: /usr/local/sbin/n5-fan-control, unit n5-fan-control.service.
SPDX-License-Identifier: GPL-2.0-only
"""
import collections
import glob
import math
import os
import signal
import socket
import sys
import time

SYSFS = os.environ.get("N5_FAN_SYSFS", "/sys")
# Prometheus textfile output, off unless set (for example in /etc/default/n5-fan-control).
PROM_DIR = os.environ.get("N5_FAN_PROM_DIR", "")
# Internal NVMe drives seen so far, each with its temperature and limit files. Kept in the
# unit's StateDirectory, so it survives service restarts and reboots: a drive or file that
# drops out, or fails to appear at boot, means full speed. After removing or replacing a
# drive on purpose, delete this file and restart the service.
SEEN_FILE = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/n5-fan-control"), "nvme-drives")
DRIVER = "minisforum_n5_it5571"
INTERVAL = 2.0
MAX_STEP_DOWN = 4      # percent per interval; speeds rise at once and fall slowly
TEMP_RANGE = (5, 120)  # anything outside is treated as a failed sensor

# The SSD fan follows the smallest margin, in C, between any internal NVMe temperature (the
# composite, temp1, and each internal sensor, temp2 and up) and that drive's own warning
# temperature: temp1_max (the drive's WCTEMP unless changed), capped at temp1_crit (CCTEMP).
# A drive that reports no warning temperature (WCTEMP 0, allowed by the NVMe specification)
# gets DEFAULT_WARNING. A warning temperature outside WARNING_RANGE counts as a failed sensor.
# The kernel (drivers/nvme/host/hwmon.c, nvme_hwmon_is_visible, v7.0) creates temp1_max only
# when WCTEMP is nonzero, temp1_crit only when CCTEMP is nonzero, and temp2 and up only for
# sensors the drive reports, all once when the drive is registered.
DEFAULT_WARNING = 70
WARNING_RANGE = (50, 100)
# While a sensor is rising, its margin is taken PREDICT_AHEAD seconds ahead, from the
# least-squares slope of its last TREND_READINGS readings less TREND_CONFIDENCE standard
# errors, so a noisy run counts only as far as it supports a rise. On N5 (8 to 10 October
# 2026) a 990 PRO composite climbed at up to 0.5 C/s when load started, and a fan increase
# showed on the drive 10 to 15 seconds later; 30 seconds covers that lag, the interval and
# the fit's own delay. At idle, the 990 EVOs' Sensor 1 jumps up to 5 C between readings: a
# plain slope made that a rise of up to 12.7 C over 30 seconds, the confidence bound 3.4 C.
TREND_READINGS = 5
PREDICT_AHEAD = 30
TREND_CONFIDENCE = 2

# (input C, duty %) points, linear between, clamped at both ends. The SSD fan's input is the
# margin to the warning temperature (smaller is hotter); the CPU fan's is its temperature.
# The internal sensors run 2 to 10 C above the composite, so 7 C below warning on the
# hottest sensor is about where the old composite curve reached full speed on the 990 PRO.
# The CPU fan has no tachometer on the N5 Air, and a by-ear test could not tell
# whether pwm1 or pwm4 drives it (each stayed at full after "automatic"), so both
# follow the CPU curve. The SSD fan (pwm2) has a tachometer.
# The CPU fan is off until the CPU needs it (it is loud even at 25%, the lowest duty
# tested and heard spinning, so there is no minimum speed): it starts when the CPU reaches the
# first curve point, stops again at off_below, and gets one interval at kick percent
# when starting from off, because a stopped fan may not start at a low duty.
FANS = {
    "ssd": {"channels": [2], "points": [(7, 100), (12, 85), (20, 60), (30, 35)]},
    "cpu": {"channels": [1, 4], "points": [(55, 25), (70, 55), (80, 80), (88, 100)],
            "off_below": 50, "kick": 60},
}


def log(msg):
    print(msg, flush=True)


def read_int(path):
    with open(path) as f:
        return int(f.read().strip())


def write_str(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def find_hwmon(name):
    for d in sorted(glob.glob(os.path.join(SYSFS, "class/hwmon/hwmon*"))):
        try:
            with open(os.path.join(d, "name")) as f:
                if f.read().strip() == name:
                    return d
        except OSError:
            continue
    return None


def usb4_attached(nvme_dir):
    """True if the controller's PCIe path passes a port tunnelled through USB4/Thunderbolt.

    The kernel links each USB4 PCIe tunnel port to its USB4 host interface as a device-link
    supplier ("supplier:pci:..." in the port's directory), and the thunderbolt driver binds
    that host interface. Without such a link the drive counts as internal, so a detection
    that misses keeps the fail-safe rather than dropping it.
    """
    top = os.path.realpath(os.path.join(SYSFS, "devices"))
    dev = os.path.realpath(os.path.join(nvme_dir, "device"))
    while dev.startswith(top + os.sep):
        for link in glob.glob(os.path.join(dev, "supplier:*")):
            if os.path.basename(os.path.realpath(os.path.join(link, "supplier/driver"))) == "thunderbolt":
                return True
        dev = os.path.dirname(dev)
    return False


def nvme_files():
    """Every internal NVMe drive's hwmon directory and the temperature and limit files in it,
    {drive: (dir, {file})}, without reading any. Drives attached through USB4 are left out, so
    they are neither recorded nor used for the SSD fan (they sit outside the case and come and
    go). Raises if a drive has no hwmon directory or there is no internal drive."""
    found = {}
    for d in sorted(glob.glob(os.path.join(SYSFS, "class/nvme/nvme*"))):
        if usb4_attached(d):
            continue
        name = os.path.basename(d)
        hw = sorted(glob.glob(os.path.join(d, "hwmon*")) + glob.glob(os.path.join(d, "device/hwmon/hwmon*")))
        if not hw:
            raise RuntimeError(f"{name} has no temperature sensor")
        files = {os.path.basename(p) for p in glob.glob(os.path.join(hw[0], "temp*_input"))}
        files |= {f for f in ("temp1_max", "temp1_crit") if os.path.exists(os.path.join(hw[0], f))}
        found[name] = (hw[0], files)
    if not found:
        raise RuntimeError("no internal NVMe temperatures found")
    return found


def nvme_sensors(found):
    """Read the files nvme_files() listed, {drive: {"warning": C, "temps": {sensor: C},
    "limits": {file: C}}}: the composite (temp1) and each internal sensor (temp2 and up), and
    the warning temperature: temp1_max, capped at temp1_crit when the drive reports one, or
    DEFAULT_WARNING when it reports no temp1_max. Raises if a listed file cannot be read."""
    drives = {}
    for name, (hw, files) in found.items():
        temps = {"temp1": read_int(os.path.join(hw, "temp1_input")) / 1000}
        temps.update({f[:-len("_input")]: read_int(os.path.join(hw, f)) / 1000
                      for f in files if f.endswith("_input") and f != "temp1_input"})
        limits = {f: read_int(os.path.join(hw, f)) / 1000 for f in ("temp1_max", "temp1_crit") if f in files}
        warning = limits.get("temp1_max", DEFAULT_WARNING)
        drives[name] = {"warning": min(warning, limits.get("temp1_crit", warning)), "temps": temps, "limits": limits}
    return drives


def cpu_temp():
    hw = find_hwmon("k10temp")
    if not hw:
        raise RuntimeError("k10temp not found")
    return read_int(os.path.join(hw, "temp1_input")) / 1000


def read_seen():
    """NVMe drives recorded so far with their files, {drive: {file}}; empty on the first start
    after installation. A line is the drive name and its files; a name alone (written by
    versions before the file list) records the drive with no files yet."""
    try:
        with open(SEEN_FILE) as f:
            return {line.split()[0]: set(line.split()[1:]) for line in f if line.strip()}
    except FileNotFoundError:
        return {}


def write_seen(record):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(" ".join([n, *sorted(record[n])]) + "\n" for n in sorted(record)))
    os.replace(tmp, SEEN_FILE)


def plausible(t):
    return TEMP_RANGE[0] <= t <= TEMP_RANGE[1]


def curve(points, temp):
    """Duty percent for a temperature, linear between points, clamped at the ends."""
    if temp <= points[0][0]:
        return points[0][1]
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if temp <= t1:
            return round(d0 + (d1 - d0) * (temp - t0) / (t1 - t0))
    return points[-1][1]


def predicted(readings):
    """Temperature to plan for from (time s, C) readings, oldest first: the latest reading,
    or while the last TREND_READINGS readings show a rise, the latest plus that rise carried
    PREDICT_AHEAD seconds ahead. A falling trend never lowers it below the latest reading."""
    latest = readings[-1][1]
    if len(readings) < TREND_READINGS:
        return latest
    window = list(readings)[-TREND_READINGS:]
    n = len(window)
    mt = sum(t for t, _ in window) / n
    my = sum(y for _, y in window) / n
    sxx = sum((t - mt) ** 2 for t, _ in window)
    if sxx <= 0:
        return latest
    slope = sum((t - mt) * (y - my) for t, y in window) / sxx
    residuals = sum((y - my - slope * (t - mt)) ** 2 for t, y in window)
    rise = slope - TREND_CONFIDENCE * math.sqrt(residuals / (n - 2) / sxx)
    return latest + rise * PREDICT_AHEAD if rise > 0 else latest


def ssd_margins(fans, drives, now):
    """Record each sensor's reading and return per drive the hottest reading, the hottest
    planned temperature (see predicted) and the smallest margin to its warning temperature,
    with the sensor that sets that margin."""
    summary = {}
    for drive, info in drives.items():
        planned = {}
        for sensor, temp in info["temps"].items():
            history = fans.history.setdefault((drive, sensor), collections.deque(maxlen=TREND_READINGS))
            history.append((now, temp))
            planned[sensor] = predicted(history)
        limiting = max(planned, key=planned.get)
        summary[drive] = {"hottest": max(info["temps"].values()), "planned": planned[limiting],
                          "warning": info["warning"], "margin": info["warning"] - planned[limiting],
                          "sensor": limiting}
    return summary


def target_for(fans, name, temp):
    """Curve duty, or 0 while a fan with an off state is off (with hysteresis)."""
    spec = FANS[name]
    if "off_below" not in spec:
        return curve(spec["points"], temp)
    if fans.running[name] and temp <= spec["off_below"]:
        fans.running[name] = False
    elif not fans.running[name] and temp >= spec["points"][0][0]:
        fans.running[name] = True
    return curve(spec["points"], temp) if fans.running[name] else 0


def next_duty(current, target):
    """Rise at once; fall by at most MAX_STEP_DOWN per interval."""
    if current is None or target >= current:
        return target
    return max(target, current - MAX_STEP_DOWN)


def to_pwm(percent):
    return max(0, min(255, round(percent * 255 / 100)))


class Fans:
    def __init__(self):
        self.hw = find_hwmon(DRIVER)
        if not self.hw:
            raise RuntimeError(f"{DRIVER} hwmon device not found")
        for spec in FANS.values():
            for node in (f"pwm{ch}{suffix}" for ch in spec["channels"] for suffix in ("", "_enable")):
                path = os.path.join(self.hw, node)
                if not os.access(path, os.W_OK):
                    raise RuntimeError(f"{path} is not writable (driver needs experimental_write=1 and writable_mask)")
        self.seen = read_seen()
        self.history = {}    # (drive, sensor): recent (time, C) readings, cleared after a fault
        self.duty = {name: None for name in FANS}
        self.running = {name: "off_below" not in spec for name, spec in FANS.items()}
        self.kicked = {name: False for name in FANS}

    def take_over(self):
        # Entering manual mode sets full speed first (driver behaviour).
        for spec in FANS.values():
            for ch in spec["channels"]:
                write_str(os.path.join(self.hw, f"pwm{ch}_enable"), 1)

    def set(self, name, percent):
        for ch in FANS[name]["channels"]:
            write_str(os.path.join(self.hw, f"pwm{ch}"), to_pwm(percent))
        self.duty[name] = percent

    def full(self):
        for name, spec in FANS.items():
            self.kicked[name] = False        # recovery then falls slowly from full
            for ch in spec["channels"]:      # each channel on its own: one failure must not skip the rest
                try:
                    write_str(os.path.join(self.hw, f"pwm{ch}"), 255)
                except OSError as e:
                    log(f"failsafe: could not set {name} fan (pwm{ch}) to full: {e}")
            self.duty[name] = 100

    def rpm(self, channel):
        try:
            return read_int(os.path.join(self.hw, f"fan{channel}_input"))
        except OSError:
            return None


def failsafe():
    """Set every controlled fan to full speed; used by ExecStopPost."""
    hw = find_hwmon(DRIVER)
    if not hw:
        log("failsafe: driver not loaded; firmware is in control")
        return 0
    rc = 0
    for name, spec in FANS.items():
        for ch in spec["channels"]:
            try:
                write_str(os.path.join(hw, f"pwm{ch}_enable"), 1)
                write_str(os.path.join(hw, f"pwm{ch}"), 255)
            except OSError as e:
                log(f"failsafe: {name} fan (pwm{ch}): {e}")
                rc = 1
    return rc


def notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(msg.encode())


def write_prom(fans, drives, summary, cpu):
    if not PROM_DIR or not os.path.isdir(PROM_DIR):
        return
    lines = ["# HELP n5_fan_duty_percent Fan duty set by n5-fan-control.", "# TYPE n5_fan_duty_percent gauge"]
    for name, duty in fans.duty.items():
        if duty is not None:
            lines.append(f'n5_fan_duty_percent{{fan="{name}"}} {duty}')
    rpm = fans.rpm(FANS["ssd"]["channels"][0])
    if rpm is not None:
        lines += ["# TYPE n5_fan_rpm gauge", f'n5_fan_rpm{{fan="ssd"}} {rpm}']
    composite = max(info["temps"]["temp1"] for info in drives.values())
    lines += ["# TYPE n5_fan_control_temp_celsius gauge", f'n5_fan_control_temp_celsius{{source="nvme_max"}} {composite}',
              f'n5_fan_control_temp_celsius{{source="cpu"}} {cpu}',
              "# HELP n5_fan_ssd_margin_celsius Smallest margin to a drive's warning temperature; sets the SSD fan.",
              "# TYPE n5_fan_ssd_margin_celsius gauge",
              f"n5_fan_ssd_margin_celsius {round(min(s['margin'] for s in summary.values()), 2)}"]
    for metric, key, text in (("hottest", "hottest", "Hottest current reading of the drive's sensors."),
                              ("predicted", "planned", "Hottest reading or, while rising, prediction the fan plans for."),
                              ("warning", "warning", "Drive's warning temperature used for its margin."),
                              ("margin", "margin", "Warning temperature less the predicted temperature.")):
        lines += [f"# HELP n5_fan_nvme_{metric}_celsius {text}", f"# TYPE n5_fan_nvme_{metric}_celsius gauge"]
        lines += [f'n5_fan_nvme_{metric}_celsius{{drive="{d}"}} {round(s[key], 2)}' for d, s in sorted(summary.items())]
    lines.append("")
    tmp = os.path.join(PROM_DIR, ".n5_fan.prom.tmp")
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, os.path.join(PROM_DIR, "n5_fan.prom"))


def tick(fans, now=None):
    """One control step. Returns the targets used; any sensor problem means full speed."""
    now = time.monotonic() if now is None else now
    try:
        found = nvme_files()
        missing = [d for d in sorted(fans.seen) if d not in found]
        missing += [f"{d} {f}" for d, (_, files) in sorted(found.items()) for f in sorted(fans.seen.get(d, set()) - files)]
        candidate = {**fans.seen, **{d: fans.seen.get(d, set()) | files for d, (_, files) in found.items()}}
        if candidate != fans.seen:
            write_seen(candidate)            # record first, even beside a fault; memory follows only on success
            fans.seen = candidate
        if missing:
            raise RuntimeError(f"{', '.join(missing)} recorded but no longer present on an internal drive")
        drives = nvme_sensors(found)
        cpu = cpu_temp()
        bad = [f"{d} {s}" for d, info in drives.items() for s, t in sorted(info["temps"].items()) if not plausible(t)]
        bad += [f"{d} {node}" for d, info in drives.items()
                for node, t in sorted({**info["limits"], "warning": info["warning"]}.items())
                if not WARNING_RANGE[0] <= t <= WARNING_RANGE[1]]
        if not plausible(cpu):
            bad.append("cpu")
        if bad:
            raise RuntimeError(f"implausible temperature from {', '.join(bad)}")
        summary = ssd_margins(fans, drives, now)
        limiting = min(summary, key=lambda d: summary[d]["margin"])
        sources = {"ssd": summary[limiting]["margin"], "cpu": cpu}
        targets = {name: target_for(fans, name, sources[name]) for name in FANS}
    except (OSError, ValueError, RuntimeError) as e:
        log(f"sensor problem, fans to full: {e}")
        fans.history.clear()                 # a trend never spans a fault
        fans.full()
        return None
    reason = {"ssd": f"margin {sources['ssd']:.0f} C to warning, {limiting} {summary[limiting]['sensor']}",
              "cpu": f"{cpu:.0f} C"}
    for name, target in targets.items():
        new = next_duty(fans.duty[name], target)
        kick = FANS[name].get("kick")
        if kick and target > 0 and not fans.duty[name]:
            new = max(target, kick)          # starting from off: one interval at kick speed
            fans.kicked[name] = True
        elif fans.kicked[name]:
            new = target                     # after the kick, straight to the curve
            fans.kicked[name] = False
        if fans.duty[name] is None or abs(new - fans.duty[name]) >= 10:
            log(f"{name} fan {fans.duty[name]} -> {new}% ({reason[name]})")
        fans.set(name, new)
    try:
        write_prom(fans, drives, summary, cpu)
    except OSError as e:
        log(f"metrics not written: {e}")
    return targets


def main():
    if "--failsafe" in sys.argv:
        return failsafe()
    fans = Fans()
    stopping = []

    def on_signal(signum, _frame):
        stopping.append(signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    fans.take_over()
    log("controlling " + ", ".join(f"{n} ({'+'.join(f'pwm{c}' for c in s['channels'])})" for n, s in FANS.items()))
    if PROM_DIR and not os.path.isdir(PROM_DIR):
        log(f"metrics directory {PROM_DIR} not found; metrics off")
    notify("READY=1")
    try:
        while not stopping:
            tick(fans)
            notify("WATCHDOG=1")
            time.sleep(INTERVAL)
    finally:
        fans.full()
        notify("STOPPING=1")
        log("stopped; controlled fans left at full speed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
