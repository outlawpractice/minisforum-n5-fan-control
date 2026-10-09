#!/usr/bin/env python3
"""Tests for n5-fan-control.py against a fake sysfs tree (no hardware).

Run from the repository root: python3 tests/test_n5_fan_control.py
SCRIPT=<path> tests a different copy (for example a mutated one, which must fail).
Assertions read the bytes the controller wrote into the fake pwm files.
"""
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fanctl", os.environ.get("SCRIPT", ROOT / "n5-fan-control.py"))
fanctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fanctl)
DEFAULT_PROM_DIR = fanctl.PROM_DIR   # before make_sysfs overrides it
DEFAULT_SEEN_FILE = fanctl.SEEN_FILE


def make_sysfs(nvme=(40, 42), cpu=45):
    root = Path(tempfile.mkdtemp())
    k10 = root / "class/hwmon/hwmon0"
    k10.mkdir(parents=True)
    (k10 / "name").write_text("k10temp\n")
    (k10 / "temp1_input").write_text(f"{round(cpu * 1000)}\n")
    ec = root / "class/hwmon/hwmon1"
    ec.mkdir(parents=True)
    (ec / "name").write_text("minisforum_n5_it5571\n")
    for ch in (1, 2, 4):
        (ec / f"pwm{ch}").write_text("255\n")
        (ec / f"pwm{ch}_enable").write_text("2\n")
    (ec / "fan2_input").write_text("2140\n")
    for i, t in enumerate(nvme):
        hw = root / f"class/nvme/nvme{i}/hwmon{10 + i}"
        hw.mkdir(parents=True)
        (hw / "temp1_input").write_text(f"{t * 1000}\n")
    prom = root / "prom"
    prom.mkdir()
    fanctl.SEEN_FILE = str(root / "nvme-drives")
    fanctl.SYSFS = str(root)
    fanctl.PROM_DIR = str(prom)
    return root, ec


def pwm(ec, ch):
    return int((ec / f"pwm{ch}").read_text())


def test_curve_clamps_and_interpolates():
    pts = fanctl.FANS["ssd"]["points"]
    assert fanctl.curve(pts, 20) == 35
    assert fanctl.curve(pts, 45) == 35
    assert fanctl.curve(pts, 50) == 48
    assert fanctl.curve(pts, 65) == 85
    assert fanctl.curve(pts, 90) == 100


def test_speed_rises_at_once_and_falls_slowly():
    assert fanctl.next_duty(None, 40) == 40
    assert fanctl.next_duty(40, 90) == 90
    assert fanctl.next_duty(90, 40) == 90 - fanctl.MAX_STEP_DOWN
    assert fanctl.next_duty(42, 40) == 40


def test_tick_sets_both_fans_from_temperatures():
    _, ec = make_sysfs(nvme=(48, 66), cpu=75)
    fans = fanctl.Fans()
    fans.take_over()
    assert all((ec / f"pwm{ch}_enable").read_text() == "1" for ch in (1, 2, 4))
    fanctl.tick(fans)
    assert pwm(ec, 2) == fanctl.to_pwm(fanctl.curve(fanctl.FANS["ssd"]["points"], 66)), "SSD fan follows the hottest NVMe"
    cpu = fanctl.to_pwm(fanctl.curve(fanctl.FANS["cpu"]["points"], 75))
    assert pwm(ec, 1) == cpu and pwm(ec, 4) == cpu, "both CPU fan channels follow Tctl"


def test_missing_nvme_sensor_means_full_speed():
    root, ec = make_sysfs()
    fans = fanctl.Fans()
    fanctl.tick(fans)
    assert pwm(ec, 2) < 255
    (root / "class/nvme/nvme1/hwmon11/temp1_input").unlink()
    fanctl.tick(fans)
    assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255


def test_nvme_without_sensor_means_full_speed():
    root, ec = make_sysfs()
    fans = fanctl.Fans()
    fanctl.tick(fans)
    assert pwm(ec, 2) < 255
    (root / "class/nvme/nvme1/hwmon11/temp1_input").unlink()
    (root / "class/nvme/nvme1/hwmon11").rmdir()
    fanctl.tick(fans)
    assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255


def test_nvme_controller_removed_means_full_speed():
    root, ec = make_sysfs(nvme=(40, 66))
    fans = fanctl.Fans()
    fanctl.tick(fans)
    shutil.rmtree(root / "class/nvme/nvme1")
    for _ in range(3):
        fanctl.tick(fans)
    assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255, "a drive seen at start must not drop out"


def test_restart_with_a_drive_missing_stays_full():
    root, ec = make_sysfs(nvme=(40, 80))
    fanctl.tick(fanctl.Fans())
    shutil.rmtree(root / "class/nvme/nvme1")
    fans = fanctl.Fans()
    fans.take_over()
    fanctl.tick(fans)
    assert pwm(ec, 2) == 255, "a fresh start must not accept fewer drives than seen before"


def test_drive_missing_after_reboot_stays_full():
    root, ec = make_sysfs(nvme=(40,))
    Path(fanctl.SEEN_FILE).write_text("nvme0\nnvme1\n")   # recorded before the reboot
    fans = fanctl.Fans()
    fans.take_over()
    fanctl.tick(fans)
    assert pwm(ec, 2) == 255, "a drive recorded before a reboot and absent now means full speed"
    # The record survives a reboot only if it lives outside /run (StateDirectory, not RuntimeDirectory).
    assert "STATE_DIRECTORY" in os.environ or DEFAULT_SEEN_FILE == "/var/lib/n5-fan-control/nvme-drives"


def test_failed_seen_write_is_retried_and_stays_full():
    root, ec = make_sysfs(nvme=(40, 42))
    fanctl.SEEN_FILE = str(root / "state" / "nvme-drives")      # directory does not exist yet
    fans = fanctl.Fans()
    for _ in range(3):
        fanctl.tick(fans)
        assert pwm(ec, 2) == 255, "unrecorded drives: full speed until the record is written"
    assert fans.seen == set(), "memory must not run ahead of the file"
    (root / "state").mkdir()
    fanctl.tick(fans)
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0\nnvme1\n"
    assert pwm(ec, 2) < 255


def test_full_speed_reaches_every_channel_when_one_write_fails():
    _, ec = make_sysfs()
    fans = fanctl.Fans()
    for ch in (1, 2, 4):
        (ec / f"pwm{ch}").write_text("60\n")
    (ec / "pwm1").unlink()
    (ec / "pwm1").mkdir()                                        # writes to pwm1 now fail
    fans.full()
    assert pwm(ec, 4) == 255 and pwm(ec, 2) == 255, "a failed pwm1 write must not stop pwm4"


def test_drive_appearing_later_is_added():
    root, ec = make_sysfs(nvme=(40,))
    fans = fanctl.Fans()
    fanctl.tick(fans)
    hw = root / "class/nvme/nvme1/hwmon11"
    hw.mkdir(parents=True)
    (hw / "temp1_input").write_text("41000\n")
    fanctl.tick(fans)
    assert pwm(ec, 2) < 255, "a new drive is not a fault"
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0\nnvme1\n"


def test_metrics_off_unless_configured():
    assert "N5_FAN_PROM_DIR" in os.environ or DEFAULT_PROM_DIR == "", "metrics must default to off"
    root, _ = make_sysfs()
    fanctl.PROM_DIR = ""
    fanctl.tick(fanctl.Fans())
    assert list((root / "prom").iterdir()) == []


def run_hwmon(dirs):
    """Run the installer's hwmon() under strict mode against a fake hwmon tree."""
    root = Path(tempfile.mkdtemp())
    for i, name in enumerate(dirs):
        d = root / f"hwmon{i}"
        d.mkdir()
        if name is not None:
            (d / "name").write_text(name + "\n")
    script = (ROOT / "install.sh").read_text()
    func = re.search(r"^hwmon\(\) \{\n.*?^\}\n", script, re.S | re.M).group(0)
    return subprocess.run(["bash", "-c", f"set -euo pipefail\nHWMON_ROOT={root}\n{func}h=$(hwmon)\necho \"$h\""],
                          capture_output=True, text=True)


def test_installer_hwmon_requires_exactly_one_readable_match():
    ec = "minisforum_n5_it5571"
    ok = run_hwmon(["k10temp", ec])
    assert ok.returncode == 0 and ok.stdout.strip().endswith("hwmon1"), ok
    for dirs in ([ec, None], [None, ec], [ec, ec], ["k10temp"]):
        assert run_hwmon(dirs).returncode != 0, dirs


def test_implausible_temperature_means_full_speed():
    for bad_nvme, bad_cpu in (((40, 0), 45), ((40, 150), 45), ((40, 41), 0)):
        _, ec = make_sysfs(nvme=bad_nvme, cpu=bad_cpu)
        fans = fanctl.Fans()
        fanctl.tick(fans)
        assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255, (bad_nvme, bad_cpu)


def test_cpu_fan_off_when_cool_and_kicked_on_start():
    _, ec = make_sysfs(nvme=(40, 41), cpu=45)
    k10 = Path(fanctl.SYSFS) / "class/hwmon/hwmon0/temp1_input"
    fans = fanctl.Fans()
    fanctl.tick(fans)
    assert pwm(ec, 1) == 0 and pwm(ec, 4) == 0, "CPU fan off below the first curve point"
    k10.write_text("53000\n")
    fanctl.tick(fans)
    assert pwm(ec, 1) == 0, "still off between off_below and the start point (hysteresis)"
    k10.write_text("60000\n")
    fanctl.tick(fans)
    kick = fanctl.to_pwm(fanctl.FANS["cpu"]["kick"])
    assert pwm(ec, 1) == kick and pwm(ec, 4) == kick, "start from off gets the kick speed"
    fanctl.tick(fans)
    assert pwm(ec, 1) == fanctl.to_pwm(fanctl.curve(fanctl.FANS["cpu"]["points"], 60)), "then the curve speed"
    k10.write_text("53000\n")
    fanctl.tick(fans)
    assert pwm(ec, 1) > 0, "stays on above off_below once running"
    k10.write_text("49000\n")
    for _ in range(15):
        fanctl.tick(fans)
    assert pwm(ec, 1) == 0 and pwm(ec, 4) == 0, "off again at or below off_below"


def test_cpu_fan_on_and_off_at_exact_thresholds():
    _, ec = make_sysfs(cpu=54.9)
    k10 = Path(fanctl.SYSFS) / "class/hwmon/hwmon0/temp1_input"
    fans = fanctl.Fans()
    fanctl.tick(fans)
    assert pwm(ec, 1) == 0, "off just below the start point"
    k10.write_text("55000\n")
    fanctl.tick(fans)
    assert pwm(ec, 1) > 0, "on at exactly the start point"
    k10.write_text("50100\n")
    for _ in range(15):
        fanctl.tick(fans)
    assert pwm(ec, 1) > 0, "still on just above off_below"
    k10.write_text("50000\n")
    for _ in range(15):
        fanctl.tick(fans)
    assert pwm(ec, 1) == 0, "off at exactly off_below"


def test_sensor_failure_during_kick_then_recovery_falls_slowly():
    _, ec = make_sysfs(cpu=45)
    k10 = Path(fanctl.SYSFS) / "class/hwmon/hwmon0/temp1_input"
    fans = fanctl.Fans()
    fanctl.tick(fans)
    k10.write_text("60000\n")
    fanctl.tick(fans)
    assert pwm(ec, 1) == fanctl.to_pwm(fanctl.FANS["cpu"]["kick"]), "kick tick"
    k10.unlink()
    fanctl.tick(fans)
    assert pwm(ec, 1) == 255, "sensor problem: full"
    k10.write_text("60000\n")
    fanctl.tick(fans)
    assert pwm(ec, 1) == fanctl.to_pwm(100 - fanctl.MAX_STEP_DOWN), "recovery falls at most one step"


def test_unit_file_fails_safe():
    unit = (ROOT / "n5-fan-control.service").read_text()
    for line in ("Type=notify", "WatchdogSec=20", "Restart=always",
                 "StateDirectory=n5-fan-control",
                 "ExecStopPost=/usr/bin/python3 /usr/local/sbin/n5-fan-control --failsafe"):
        assert line in unit.splitlines(), line


def test_failsafe_command_sets_manual_full_speed():
    _, ec = make_sysfs()
    for ch in (1, 2, 4):
        (ec / f"pwm{ch}").write_text("60\n")
    assert fanctl.failsafe() == 0
    for ch in (1, 2, 4):
        assert (ec / f"pwm{ch}_enable").read_text() == "1"
        assert pwm(ec, ch) == 255


def test_refuses_to_start_without_writable_pwm():
    for ch in (2, 4):
        _, ec = make_sysfs()
        (ec / f"pwm{ch}").chmod(stat.S_IRUSR)
        try:
            fanctl.Fans()
        except RuntimeError as e:
            assert "not writable" in str(e)
        else:
            raise AssertionError(f"started without a writable pwm{ch}")


def test_metrics_file_written():
    root, _ = make_sysfs(nvme=(50, 60), cpu=58)
    fans = fanctl.Fans()
    fanctl.tick(fans)
    text = (root / "prom/n5_fan.prom").read_text()
    assert 'n5_fan_duty_percent{fan="ssd"}' in text and 'n5_fan_rpm{fan="ssd"} 2140' in text
    assert 'n5_fan_control_temp_celsius{source="nvme_max"} 60.0' in text


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"{failed} failed" if failed else "all passed")
    sys.exit(1 if failed else 0)
