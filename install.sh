#!/bin/bash
# Install n5-fan-control on a Minisforum N5 Air (board F8NAB). Run as root from the
# repository directory, after installing the patched driver (README.md):
#   sudo ./install.sh
# Allows driver writes on the CPU fan channels (pwm1, pwm4) and the SSD fan (pwm2) only,
# installs the controller, and enables n5-fan-control (fail-safe: full speed).
# Stops at the first failed step; exits 0 only when the controller is running.
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
HWMON_ROOT=${HWMON_ROOT:-/sys/class/hwmon}   # overridden only by tests
hwmon() {
  local found=() x name
  for x in "$HWMON_ROOT"/hwmon*; do
    name=$(cat "$x/name") || { echo "cannot read $x/name" >&2; return 1; }
    [ "$name" = minisforum_n5_it5571 ] && found+=("$x")
  done
  [ "${#found[@]}" -eq 1 ] || { echo "expected one minisforum_n5_it5571 hwmon device, found ${#found[@]}" >&2; return 1; }
  echo "${found[0]}"
}

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
board=$(cat /sys/class/dmi/id/board_name)
[ "$board" = F8NAB ] || { echo "board is $board; the fan channels were measured only on the N5 Air (F8NAB)" >&2; exit 1; }
modinfo -F parm minisforum_n5_it5571 | grep '^writable_mask:' >/dev/null \
  || { echo "minisforum_n5_it5571 without the writable_mask patch; see README.md" >&2; exit 1; }

install -m 0755 -o root -g root "$SRC/n5-fan-control.py" /usr/local/sbin/n5-fan-control
install -m 0644 -o root -g root "$SRC/n5-fan-control.service" /etc/systemd/system/n5-fan-control.service
[ -e /etc/default/n5-fan-control ] || install -m 0644 -o root -g root "$SRC/n5-fan-control.default" /etc/default/n5-fan-control
printf '%s\n' '# n5-fan-control owns the CPU fan (pwm1 and pwm4) and the SSD fan (pwm2); pwm3 stays read-only.' \
  'options minisforum_n5_it5571 experimental_write=1 writable_mask=0xb' > /etc/modprobe.d/n5-fan-control.conf
echo minisforum_n5_it5571 > /etc/modules-load.d/n5-fan-control.conf
systemctl daemon-reload
# Stopping first leaves the fans at full speed while the driver reloads with the new options.
if systemctl is-active --quiet n5-fan-control; then systemctl stop n5-fan-control; fi
modprobe -r minisforum_n5_it5571
modprobe minisforum_n5_it5571
systemctl enable --now n5-fan-control
sleep 12
journalctl -u n5-fan-control --no-pager -n 6 -o cat
h=$(hwmon)
# Masked channels have no pwm node, so presence proves the mask; pwm3 must stay absent.
[ "$(cat /sys/module/minisforum_n5_it5571/parameters/writable_mask)" = 11 ] || { echo "writable_mask is not 0xb" >&2; exit 1; }
for n in pwm1 pwm2 pwm4; do [ -e "$h/$n" ] || { echo "$h/$n missing" >&2; exit 1; }; done
[ ! -e "$h/pwm3" ] || { echo "$h/pwm3 should not exist" >&2; exit 1; }
systemctl is-active --quiet n5-fan-control || { echo "n5-fan-control is not active" >&2; exit 1; }
echo "n5-fan-control active; SSD fan now $(cat "$h/fan2_input") rpm"
