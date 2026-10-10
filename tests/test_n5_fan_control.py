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


def make_sysfs(nvme=(40, 42), cpu=45, warning=None):
    """Each nvme entry is a composite temperature, or a tuple of the composite and its sensors.
    warning: one temp1_max for every drive, a tuple of them per drive, or None for none."""
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
        for k, temp in enumerate(t if isinstance(t, tuple) else (t,), 1):
            (hw / f"temp{k}_input").write_text(f"{round(temp * 1000)}\n")
        w = warning[i] if isinstance(warning, tuple) else warning
        if w is not None:
            (hw / "temp1_max").write_text(f"{round(w * 1000)}\n")
    prom = root / "prom"
    prom.mkdir()
    fanctl.SEEN_FILE = str(root / "nvme-drives")
    fanctl.SYSFS = str(root)
    fanctl.PROM_DIR = str(prom)
    return root, ec


def add_nvme(root, name, temp, port, supplier_driver=None, sensor=True):
    """A controller laid out as in real sysfs, below PCI root port `port`. With supplier_driver,
    the port gets a device-link supplier bound to that driver: the kernel links each USB4 PCIe
    tunnel port to its USB4 host interface, which the "thunderbolt" driver binds.
    Returns the PCI device directory; removing it and the class link is an unplug."""
    devices = root / "devices/pci0000:00"
    pci = devices / port / "0000:72:00.0" / f"pci-{name}"
    ctl = pci / "nvme" / name
    ctl.mkdir(parents=True)
    (ctl / "device").symlink_to("../..")
    if sensor:
        (ctl / "hwmon20").mkdir()
        (ctl / "hwmon20/temp1_input").write_text(f"{temp * 1000}\n")
    (root / "class/nvme").mkdir(parents=True, exist_ok=True)
    (root / "class/nvme" / name).symlink_to(ctl)
    if supplier_driver:
        supplier = "0000:d4:00.6" if supplier_driver == "thunderbolt" else "0000:d4:00.3"
        nhi = devices / "0000:00:08.3" / supplier
        nhi.mkdir(parents=True, exist_ok=True)
        driver = root / "bus/pci/drivers" / supplier_driver
        driver.mkdir(parents=True, exist_ok=True)
        if not (nhi / "driver").is_symlink():
            (nhi / "driver").symlink_to(driver)
        link = root / f"devices/virtual/devlink/pci:{supplier}--pci:{port}"
        if not link.exists():
            link.mkdir(parents=True)
            (link / "supplier").symlink_to(nhi)
            (devices / port / f"supplier:pci:{supplier}").symlink_to(link)
    return pci


def unplug(root, name, pci):
    (root / "class/nvme" / name).unlink()
    shutil.rmtree(pci)


def pwm(ec, ch):
    return int((ec / f"pwm{ch}").read_text())


def ssd_pwm(margin):
    """The SSD fan's pwm value for a margin to the warning temperature, with no trend."""
    return fanctl.to_pwm(fanctl.curve(fanctl.FANS["ssd"]["points"], margin))


def set_temp(root, drive, sensor, temp):
    hw = next((root / "class/nvme" / drive).glob("hwmon*"))
    (hw / f"{sensor}_input").write_text(f"{round(temp * 1000)}\n")


def test_curve_clamps_and_interpolates():
    pts = fanctl.FANS["ssd"]["points"]
    assert fanctl.curve(pts, 40) == 35
    assert fanctl.curve(pts, 30) == 35
    assert fanctl.curve(pts, 25) == 48
    assert fanctl.curve(pts, 12) == 85
    assert fanctl.curve(pts, 7) == 100
    assert fanctl.curve(pts, -5) == 100


def test_speed_rises_at_once_and_falls_slowly():
    assert fanctl.next_duty(None, 40) == 40
    assert fanctl.next_duty(40, 90) == 90
    assert fanctl.next_duty(90, 40) == 90 - fanctl.MAX_STEP_DOWN
    assert fanctl.next_duty(42, 40) == 40


def test_steady_temperatures_follow_the_margin_curve():
    _, ec = make_sysfs(nvme=((50, 56, 52), (45, 47, 46)), warning=(82, 85))
    fans = fanctl.Fans()
    for i in range(8):
        assert fanctl.tick(fans, now=2.0 * i)["ssd"] == fanctl.curve(fanctl.FANS["ssd"]["points"], 82 - 56), i
        assert pwm(ec, 2) == ssd_pwm(82 - 56), i


def test_rising_trend_raises_the_duty_before_the_reading_does():
    root, ec = make_sysfs(nvme=((50, 52, 51),), warning=82)
    fans = fanctl.Fans()
    for i in range(fanctl.TREND_READINGS):
        set_temp(root, "nvme0", "temp2", 52 + i)                      # 0.5 C/s
        fanctl.tick(fans, now=2.0 * i)
        if i < fanctl.TREND_READINGS - 1:
            assert pwm(ec, 2) == ssd_pwm(82 - (52 + i)), "no trend before TREND_READINGS readings"
    latest = 52 + fanctl.TREND_READINGS - 1
    ahead = latest + 0.5 * fanctl.PREDICT_AHEAD
    assert pwm(ec, 2) == ssd_pwm(82 - ahead), "a steady rise is planned for PREDICT_AHEAD seconds ahead"
    assert pwm(ec, 2) > ssd_pwm(82 - latest)
    prom = (root / "prom/n5_fan.prom").read_text()
    assert f'n5_fan_nvme_hottest_celsius{{drive="nvme0"}} {float(latest)}' in prom
    assert f'n5_fan_nvme_predicted_celsius{{drive="nvme0"}} {ahead}' in prom
    assert f'n5_fan_nvme_margin_celsius{{drive="nvme0"}} {82 - ahead}' in prom
    assert f"n5_fan_ssd_margin_celsius {82 - ahead}" in prom


def test_noisy_readings_do_not_count_as_a_trend():
    # Sensor 1 of the 990 EVOs on N5 jumps up to 5 C between readings at idle.
    root, ec = make_sysfs(nvme=((41, 45, 41),), warning=75)
    fans = fanctl.Fans()
    for i, t in enumerate((45, 48, 45, 48, 48)):
        set_temp(root, "nvme0", "temp2", t)
        fanctl.tick(fans, now=2.0 * i)
    assert pwm(ec, 2) == ssd_pwm(75 - 48)


def test_falling_trend_never_lowers_the_duty_faster_than_max_step_down():
    points = fanctl.FANS["ssd"]["points"]
    root, _ = make_sysfs(nvme=((60, 74),), warning=82)
    fans = fanctl.Fans()
    for i in range(12):                                                # fast fall, 1 C/s from margin 8
        before = fans.duty["ssd"]
        set_temp(root, "nvme0", "temp2", 74 - 2 * i)
        fanctl.tick(fans, now=2.0 * i)
        if before is not None:
            assert 0 <= before - fans.duty["ssd"] <= fanctl.MAX_STEP_DOWN, i
        assert fans.duty["ssd"] >= fanctl.curve(points, 82 - (74 - 2 * i)), i
    root, _ = make_sysfs(nvme=((50, 58),), warning=82)
    fans = fanctl.Fans()
    for i in range(12):                                                # slow fall, 0.25 C/s from margin 24
        set_temp(root, "nvme0", "temp2", 58 - 0.5 * i)
        fanctl.tick(fans, now=2.0 * i)
        assert fans.duty["ssd"] == fanctl.curve(points, 82 - (58 - 0.5 * i)), "a fall is never predicted ahead"


def test_hottest_sensor_not_the_composite_sets_the_duty():
    root, ec = make_sysfs(nvme=((55, 66, 58), (60,)), warning=82)
    fanctl.tick(fanctl.Fans(), now=0)
    assert pwm(ec, 2) == ssd_pwm(82 - 66)
    prom = (root / "prom/n5_fan.prom").read_text()
    assert 'n5_fan_nvme_hottest_celsius{drive="nvme0"} 66.0' in prom
    assert 'n5_fan_control_temp_celsius{source="nvme_max"} 60.0' in prom, "nvme_max stays the hottest composite"


def test_each_drive_is_measured_against_its_own_warning_temperature():
    _, ec = make_sysfs(nvme=((60, 64), (55, 58)), warning=(85, 70))   # nvme0 hotter, nvme1 nearer its limit
    fanctl.tick(fanctl.Fans(), now=0)
    assert pwm(ec, 2) == ssd_pwm(70 - 58)
    root, ec = make_sysfs(nvme=((60, 64),), warning=85)
    (root / "class/nvme/nvme0/hwmon10/temp1_crit").write_text("80000\n")
    fanctl.tick(fanctl.Fans(), now=0)
    assert pwm(ec, 2) == ssd_pwm(80 - 64), "temp1_crit caps the warning temperature"
    root, ec = make_sysfs(nvme=((50, 52),))
    fanctl.tick(fanctl.Fans(), now=0)
    assert pwm(ec, 2) == ssd_pwm(fanctl.DEFAULT_WARNING - 52), "no temp1_max: DEFAULT_WARNING"
    assert 'n5_fan_nvme_warning_celsius{drive="nvme0"} 70' in (root / "prom/n5_fan.prom").read_text()


def test_sensor_and_warning_problems_mean_full_speed():
    def break_temp2_hot(hw): (hw / "temp2_input").write_text("150000\n")
    def break_temp2_zero_kelvin(hw): (hw / "temp2_input").write_text("-273150\n")
    def break_warning_zero(hw): (hw / "temp1_max").write_text("0\n")
    def break_warning_unset(hw): (hw / "temp1_max").write_text("65261850\n")
    def break_warning_unreadable(hw):
        (hw / "temp1_max").unlink()
        (hw / "temp1_max").mkdir()
    def break_temp2_gone(hw): (hw / "temp2_input").unlink()
    def break_max_gone(hw): (hw / "temp1_max").unlink()               # would fall back to DEFAULT_WARNING
    def break_crit_gone(hw): (hw / "temp1_crit").unlink()             # would drop the cap
    def break_crit_zero(hw): (hw / "temp1_crit").write_text("0\n")
    for breaker in (break_temp2_hot, break_temp2_zero_kelvin, break_warning_zero, break_warning_unset,
                    break_warning_unreadable, break_temp2_gone, break_max_gone, break_crit_gone, break_crit_zero):
        root, ec = make_sysfs(nvme=((50, 55, 52), (45,)), warning=82)
        (root / "class/nvme/nvme0/hwmon10/temp1_crit").write_text("85000\n")
        fans = fanctl.Fans()
        fanctl.tick(fans, now=0)
        assert pwm(ec, 2) < 255
        breaker(root / "class/nvme/nvme0/hwmon10")
        for i in range(1, 4):
            fanctl.tick(fans, now=2.0 * i)
            assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255, (breaker.__name__, i)
    root, ec = make_sysfs(nvme=((50, 55, 52),), warning=82)
    break_warning_unreadable(root / "class/nvme/nvme0/hwmon10")         # unreadable from the first read
    fanctl.tick(fanctl.Fans(), now=0)
    assert pwm(ec, 2) == 255, "an unreadable temp1_max is not a missing one"


def test_files_seen_in_a_failed_tick_are_still_remembered():
    def fail_nvme1(root, hw0): (root / "class/nvme/nvme1/hwmon11/temp1_input").write_text("bad\n")
    def heal_nvme1(root, hw0): (root / "class/nvme/nvme1/hwmon11/temp1_input").write_text("45000\n")
    def fail_crit(root, hw0):
        (hw0 / "temp1_crit").unlink()
        (hw0 / "temp1_crit").mkdir()
    def heal_crit(root, hw0):
        (hw0 / "temp1_crit").rmdir()
        (hw0 / "temp1_crit").write_text("85000\n")
    for fail, heal, vanish in ((fail_nvme1, heal_nvme1, "temp1_max"), (fail_nvme1, heal_nvme1, "temp1_crit"),
                               (fail_crit, heal_crit, "temp1_max"), (fail_nvme1, heal_nvme1, "drive")):
        root, ec = make_sysfs(nvme=((50, 55, 52), (45,)), warning=(60, 82))
        hw0 = root / "class/nvme/nvme0/hwmon10"
        (hw0 / "temp1_crit").write_text("85000\n")
        fans = fanctl.Fans()
        fail(root, hw0)
        assert fanctl.tick(fans, now=0) is None, "the first tick fails part way"
        heal(root, hw0)
        if vanish == "drive":
            shutil.rmtree(root / "class/nvme/nvme0")
        else:
            (hw0 / vanish).unlink()
        for i in range(1, 4):
            fanctl.tick(fans, now=2.0 * i)
            assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255, (fail.__name__, vanish, i)


def test_files_listed_beside_a_missing_input_are_still_recorded():
    def hide(path): path.rename(path.parent.parent / f"{path.name}.away")     # outside every glob
    def unhide(path): (path.parent.parent / f"{path.name}.away").rename(path)
    for case in ("old record, drive missing", "new limit, sensor missing", "new limit, drive missing"):
        root, ec = make_sysfs(nvme=((50, 55, 52), (45,)), warning=(None, 82))
        hw0 = root / "class/nvme/nvme0/hwmon10"
        nvme1 = root / "class/nvme/nvme1"
        if case.startswith("old record"):
            (hw0 / "temp1_max").write_text("60000\n")
            Path(fanctl.SEEN_FILE).write_text("nvme0\nnvme1\n")         # as installed on N5 today
            hidden = nvme1
        else:
            fanctl.tick(fanctl.Fans(), now=0)                            # record without nvme0 temp1_max
            (hw0 / "temp1_max").write_text("60000\n")                    # appears while something is missing
            hidden = hw0 / "temp3_input" if "sensor" in case else nvme1
        hide(hidden)
        fans = fanctl.Fans()
        assert fanctl.tick(fans, now=2) is None, case
        unhide(hidden)
        (hw0 / "temp1_max").unlink()
        for i in range(2, 5):
            fanctl.tick(fans, now=2.0 * i)
            assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255, (case, i)


def test_recorded_files_survive_a_restart_and_old_records_upgrade():
    root, ec = make_sysfs(nvme=((50, 55, 52), (45,)), warning=82)
    hw0 = root / "class/nvme/nvme0/hwmon10"
    fanctl.tick(fanctl.Fans(), now=0)
    assert Path(fanctl.SEEN_FILE).read_text() == \
        "nvme0 temp1_input temp1_max temp2_input temp3_input\nnvme1 temp1_input temp1_max\n"
    for vanish in ("temp1_max", "temp3_input"):
        (hw0 / vanish).rename(hw0 / f"{vanish}.away")
        fans = fanctl.Fans()                                           # a service restart or a reboot
        fans.take_over()
        fanctl.tick(fans, now=2)
        assert pwm(ec, 2) == 255, f"{vanish} recorded before the restart and missing now"
        (hw0 / f"{vanish}.away").rename(hw0 / vanish)
    Path(fanctl.SEEN_FILE).write_text("nvme0\nnvme1\n")                 # written by a version before file lists
    fanctl.tick(fanctl.Fans(), now=4)
    assert pwm(ec, 2) == ssd_pwm(82 - 55), "an old record is not a fault"
    assert Path(fanctl.SEEN_FILE).read_text() == \
        "nvme0 temp1_input temp1_max temp2_input temp3_input\nnvme1 temp1_input temp1_max\n"


def test_trend_does_not_span_a_fault():
    root, _ = make_sysfs(nvme=((50, 52),), warning=82)
    fans = fanctl.Fans()
    for i in range(4):
        fanctl.tick(fans, now=2.0 * i)
    set_temp(root, "nvme0", "temp2", 150)
    assert fanctl.tick(fans, now=8) is None
    set_temp(root, "nvme0", "temp2", 62)
    assert fanctl.tick(fans, now=10)["ssd"] == fanctl.curve(fanctl.FANS["ssd"]["points"], 82 - 62)


def test_tick_sets_both_fans_from_temperatures():
    _, ec = make_sysfs(nvme=(48, 66), cpu=75, warning=82)
    fans = fanctl.Fans()
    fans.take_over()
    assert all((ec / f"pwm{ch}_enable").read_text() == "1" for ch in (1, 2, 4))
    fanctl.tick(fans)
    assert pwm(ec, 2) == ssd_pwm(82 - 66), "SSD fan follows the hottest NVMe"
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
    assert fans.seen == {}, "memory must not run ahead of the file"
    (root / "state").mkdir()
    fanctl.tick(fans)
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0 temp1_input\nnvme1 temp1_input\n"
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
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0 temp1_input\nnvme1 temp1_input\n"


def test_usb4_drive_appearing_and_disappearing_keeps_the_curve():
    root, ec = make_sysfs(nvme=(40, 42))
    fans = fanctl.Fans()
    fanctl.tick(fans)
    curve_42 = ssd_pwm(fanctl.DEFAULT_WARNING - 42)
    pci = add_nvme(root, "nvme5", 41, "0000:00:04.1", "thunderbolt", sensor=False)
    fanctl.tick(fans)
    assert pwm(ec, 2) == curve_42, "a USB4 drive still enumerating (no sensor yet) is not a fault"
    (pci / "nvme/nvme5/hwmon20").mkdir()
    (pci / "nvme/nvme5/hwmon20/temp1_input").write_text("41000\n")
    fanctl.tick(fans)
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0 temp1_input\nnvme1 temp1_input\n", "a USB4 drive is not recorded"
    unplug(root, "nvme5", pci)
    pci = add_nvme(root, "nvme6", 41, "0000:00:04.1", "thunderbolt")    # replugged under a new name
    fanctl.tick(fans)
    unplug(root, "nvme6", pci)
    for _ in range(3):
        fanctl.tick(fans)
    assert pwm(ec, 2) == curve_42 and pwm(ec, 1) != 255, "unplugging a USB4 drive is not a fault"
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0 temp1_input\nnvme1 temp1_input\n"


def test_internal_drive_disappearing_beside_a_usb4_drive_means_full_speed():
    root, ec = make_sysfs(nvme=(40,))
    internal = add_nvme(root, "nvme1", 42, "0000:00:01.4", "xhci_hcd")   # a supplier, but not a USB4 host
    add_nvme(root, "nvme2", 41, "0000:00:04.1", "thunderbolt")
    fans = fanctl.Fans()
    fanctl.tick(fans)
    assert Path(fanctl.SEEN_FILE).read_text() == "nvme0 temp1_input\nnvme1 temp1_input\n"
    assert pwm(ec, 2) < 255
    unplug(root, "nvme1", internal)
    fanctl.tick(fans)
    assert pwm(ec, 1) == 255 and pwm(ec, 2) == 255 and pwm(ec, 4) == 255, "a recorded internal drive must not drop out"


def test_usb4_drive_temperature_does_not_drive_the_ssd_fan():
    for usb4_temp in (66, 150):                                        # hot, and implausible
        root, ec = make_sysfs(nvme=(40, 48))
        add_nvme(root, "nvme2", usb4_temp, "0000:00:04.1", "thunderbolt")
        fans = fanctl.Fans()
        fanctl.tick(fans)
        assert pwm(ec, 2) == ssd_pwm(fanctl.DEFAULT_WARNING - 48), usb4_temp
        assert 'source="nvme_max"} 48.0' in (root / "prom/n5_fan.prom").read_text()


def test_recorded_internal_name_now_on_usb4_means_full_speed():
    root, ec = make_sysfs(nvme=(40, 42))
    Path(fanctl.SEEN_FILE).write_text("nvme0\nnvme1\nnvme2\n")
    add_nvme(root, "nvme2", 41, "0000:00:04.1", "thunderbolt")          # the internal nvme2 is gone; a USB4 drive took its name
    fans = fanctl.Fans()
    fanctl.tick(fans)
    assert pwm(ec, 2) == 255, "a USB4 drive must not stand in for a recorded internal one"


def test_only_usb4_drives_means_full_speed():
    root, ec = make_sysfs(nvme=())
    add_nvme(root, "nvme0", 41, "0000:00:04.1", "thunderbolt")
    fanctl.tick(fanctl.Fans())
    assert pwm(ec, 2) == 255, "no internal temperature to follow"


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
