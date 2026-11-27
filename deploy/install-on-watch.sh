#!/bin/sh
# Install the app-bundle installer and its systemd user units on the watch over USB ADB.
# Idempotent. Usage: deploy/install-on-watch.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
lib=/home/ceres/.local/lib/asteroid-deploy
units=/home/ceres/.config/systemd/user
"$here/../adb-sh" "umask 022 && mkdir -p $lib $units"
adb push -q "$here/app_bundle.py" "$here/asteroid_app_install.py" "$lib/" >/dev/null
adb push -q "$here/systemd/asteroid-app-deploy.path" "$here/systemd/asteroid-app-deploy.service" "$units/" >/dev/null
# adb push creates world-writable files; this code runs as ceres, so pin owner and modes.
"$here/../adb-sh" "mkdir -p /home/ceres/.local/share/asteroid-deploy/incoming \
  && chown -R ceres:ceres /home/ceres/.local/lib/asteroid-deploy /home/ceres/.local/share/asteroid-deploy \
     $units/asteroid-app-deploy.path $units/asteroid-app-deploy.service \
  && chmod 755 $lib /home/ceres/.local/share/asteroid-deploy && chmod 644 $lib/app_bundle.py $units/asteroid-app-deploy.path $units/asteroid-app-deploy.service \
  && chmod 755 $lib/asteroid_app_install.py && chmod 700 /home/ceres/.local/share/asteroid-deploy/incoming \
  && runuser -u ceres -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
     sh -c 'systemctl --user daemon-reload && systemctl --user enable --now asteroid-app-deploy.path >/dev/null 2>&1 && systemctl --user is-active asteroid-app-deploy.path'" >/dev/null
