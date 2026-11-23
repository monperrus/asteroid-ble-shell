# App deployment over BLE

Install a QML app on the watch with nothing but the BLE file transfer service:

```sh
python3 client/watch-fs.py 43:43:A1:12:1F:AC install ../app-tabata --name Tabata --icon ios-flame-outline
```

`watch-fs install` packs a `.qml` file or an app directory into an
`asteroid-app-bundle-v1` (`.aab`, `app_bundle.py`; a `launcher.desktop` running
`~/.local/bin/qml-run` is generated unless the app ships one). It uploads the
bundle as `incoming/<app>.part`, renames it to `incoming/<app>.aab`, and waits
for `incoming/<app>.result`. Any client that can write and rename files can do
the same, e.g. Adafruit's own.

On the watch, `asteroid-app-deploy.path` (systemd user unit) sees the `.aab` and
runs `asteroid_app_install.py --incoming` as `ceres`:

1. parse the bundle strictly (header, canonical manifest, paths, modes, every
   SHA-256, no trailing bytes) and check `launcher.desktop`;
2. extract into a private temporary tree, read every file back, apply modes,
   verify the tree again, `fsync`, rename it to `releases/<manifest-sha256>`
   (an identical existing release is verified and reused);
3. switch `apps/<app>/current` with one symlink rename, point
   `~/.local/share/applications/<app>.desktop` at `current/launcher.desktop`,
   restart `asteroid-launcher`; if that fails, switch back;
4. keep the current and previous release, delete older ones, write the JSON
   verdict to `.result` and remove the `.aab` (also on failure).

A bad bundle never touches the active release. The format and the reasoning
behind it are in the watch repo's `dfu-deployment-design.md`; the Nordic DFU
transport described there is not used.

Set up the watch once over USB ADB (idempotent):

```sh
deploy/install-on-watch.sh
```

It copies `app_bundle.py` and `asteroid_app_install.py` to
`~/.local/lib/asteroid-deploy/`, the units to `~/.config/systemd/user/`, and
enables the path unit. Anyone who can write files over BLE (a paired, trusted
host) can install apps this way, which is no more than the file service already
allows.
