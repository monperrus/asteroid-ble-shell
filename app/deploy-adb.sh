#!/bin/sh
# Install the BLE UART control tile through USB ADB. No root files are changed.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
name=ble-uart
qml_dir=/home/ceres/.local/share/qml-apps
bin_dir=/home/ceres/.local/bin
desktop_dir=/home/ceres/.local/share/applications

adb push "$here/ble-uart.qml" "/data/local/tmp/$name.qml"
adb push "$here/qml-run" "/data/local/tmp/qml-run"
adb push "$here/ble-uart.desktop" "/data/local/tmp/$name.desktop"
adb shell "set -e
mkdir -p $qml_dir $bin_dir $desktop_dir
mv /data/local/tmp/$name.qml $qml_dir/$name.qml
if [ ! -x $bin_dir/qml-run ]; then mv /data/local/tmp/qml-run $bin_dir/qml-run; chmod 755 $bin_dir/qml-run; else rm -f /data/local/tmp/qml-run; fi
mv /data/local/tmp/$name.desktop $desktop_dir/$name.desktop
chown ceres:ceres $qml_dir/$name.qml $bin_dir/qml-run $desktop_dir/$name.desktop
su -s /bin/sh ceres -c '
set -e
XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user restart asteroid-launcher.service
'
"

echo "installed BLE UART tile; open the launcher and tap BLE UART"
