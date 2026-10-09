#!/usr/bin/env python3
"""Minisforum N5 Air fan controller: drives the SSD and CPU fans from Linux.

The firmware's fan curves suit a hard-drive NAS: under sustained NVMe reads the SSD
fan stayed near 2,200 rpm while a Samsung 990 PRO in a motherboard M.2 slot reached
80 C. This service sets the SSD fan from the hottest NVMe drive and the CPU fan from
the CPU temperature, through the community minisforum_n5_it5571 driver with the
writable_mask patch in driver/ (write access only to the channels named below).
See README.md for measurements, installation and configuration.

Fail-safe: a missing or implausible temperature, a drive that drops out, a failed
write, a stop or a crash all leave the controlled fans at full speed. The CPU fan
channel does not return to firmware control on this board, so full speed is also
the state after the service ends. `--failsafe` sets full speed and exits; the unit
runs it on every stop.

Install: /usr/local/sbin/n5-fan-control, unit n5-fan-control.service.
SPDX-License-Identifier: GPL-2.0-only
"""
import glob
import os
import signal
import socket
import sys
import time

SYSFS = os.environ.get("N5_FAN_SYSFS", "/sys")
# Prometheus textfile output, off unless set (for example in /etc/default/n5-fan-control).
PROM_DIR = os.environ.get("N5_FAN_PROM_DIR", "")
# NVMe drives seen so far. Kept in the unit's StateDirectory, so it survives service
# restarts and reboots: a drive that drops out, or fails to appear at boot, means full
# speed. After removing a drive on purpose, delete this file and restart the service.
SEEN_FILE = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/n5-fan-control"), "nvme-drives")
DRIVER = "minisforum_n5_it5571"
INTERVAL = 2.0
MAX_STEP_DOWN = 4      # percent per interval; speeds rise at once and fall slowly
TEMP_RANGE = (5, 120)  # anything outside is treated as a failed sensor

# (temperature C, duty %) points, linear between, clamped at both ends.
# The CPU fan has no tachometer on the N5 Air, and a by-ear test could not tell
# whether pwm1 or pwm4 drives it (each stayed at full after "automatic"), so both
# follow the CPU curve. The SSD fan (pwm2) has a tachometer.
# The CPU fan is off until the CPU needs it (it is loud even at 25%, the lowest duty
# tested and heard spinning, so there is no minimum speed): it starts when the CPU reaches the
# first curve point, stops again at off_below, and gets one interval at kick percent
# when starting from off, because a stopped fan may not start at a low duty.
FANS = {
    "ssd": {"channels": [2], "points": [(45, 35), (55, 60), (65, 85), (70, 100)]},
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


def nvme_temps():
    """Composite temperature of every NVMe drive, in C. Raises if none can be read."""
    temps = {}
    for d in sorted(glob.glob(os.path.join(SYSFS, "class/nvme/nvme*"))):
        hw = sorted(glob.glob(os.path.join(d, "hwmon*")) + glob.glob(os.path.join(d, "device/hwmon/hwmon*")))
        if not hw:
            raise RuntimeError(f"{os.path.basename(d)} has no temperature sensor")
        temps[os.path.basename(d)] = read_int(os.path.join(hw[0], "temp1_input")) / 1000
    if not temps:
        raise RuntimeError("no NVMe temperatures found")
    return temps


def cpu_temp():
    hw = find_hwmon("k10temp")
    if not hw:
        raise RuntimeError("k10temp not found")
    return read_int(os.path.join(hw, "temp1_input")) / 1000


def read_seen():
    """NVMe drives recorded so far; empty on the first start after installation."""
    try:
        with open(SEEN_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def write_seen(names):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(f"{n}\n" for n in sorted(names)))
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


def write_prom(fans, temps, cpu):
    if not PROM_DIR or not os.path.isdir(PROM_DIR):
        return
    lines = ["# HELP n5_fan_duty_percent Fan duty set by n5-fan-control.", "# TYPE n5_fan_duty_percent gauge"]
    for name, duty in fans.duty.items():
        if duty is not None:
            lines.append(f'n5_fan_duty_percent{{fan="{name}"}} {duty}')
    rpm = fans.rpm(FANS["ssd"]["channels"][0])
    if rpm is not None:
        lines += ["# TYPE n5_fan_rpm gauge", f'n5_fan_rpm{{fan="ssd"}} {rpm}']
    lines += ["# TYPE n5_fan_control_temp_celsius gauge", f'n5_fan_control_temp_celsius{{source="nvme_max"}} {max(temps.values())}',
              f'n5_fan_control_temp_celsius{{source="cpu"}} {cpu}', ""]
    tmp = os.path.join(PROM_DIR, ".n5_fan.prom.tmp")
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, os.path.join(PROM_DIR, "n5_fan.prom"))


def tick(fans):
    """One control step. Returns the targets used; any sensor problem means full speed."""
    try:
        temps = nvme_temps()
        missing = fans.seen - set(temps)
        if missing:
            raise RuntimeError(f"{', '.join(sorted(missing))} recorded but no longer present")
        if set(temps) - fans.seen:
            candidate = fans.seen | set(temps)
            write_seen(candidate)            # record first; memory follows only on success
            fans.seen = candidate
        cpu = cpu_temp()
        bad = [k for k, t in temps.items() if not plausible(t)] + ([] if plausible(cpu) else ["cpu"])
        if bad:
            raise RuntimeError(f"implausible temperature from {', '.join(bad)}")
        sources = {"ssd": max(temps.values()), "cpu": cpu}
        targets = {name: target_for(fans, name, sources[name]) for name in FANS}
    except (OSError, ValueError, RuntimeError) as e:
        log(f"sensor problem, fans to full: {e}")
        fans.full()
        return None
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
            log(f"{name} fan {fans.duty[name]} -> {new}% ({sources[name]:.0f} C)")
        fans.set(name, new)
    try:
        write_prom(fans, temps, cpu)
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
