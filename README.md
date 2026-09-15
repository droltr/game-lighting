# Game-aware RGB lighting

Motherboard (MSI MYSTIC LIGHT) and RAM (ENE DRAM) continuously follow CPU
temperature. When the focused context changes, keyboard and mouse briefly
show that same temperature color and then settle on either a per-game layout
or the configurable desktop coding layout. Game and desktop profiles never
interrupt motherboard or RAM temperature updates.

## Why this exists / what was evaluated first

No existing project does this combination (OpenRGB + KDE focused-window
detection + per-key game layout) out of the box, so this is a small,
purpose-built script rather than a fork of one repository. Two existing
projects were evaluated and one is reused as-is:

- **[FocusNotifier](https://github.com/c-massie/FocusNotifier)** (MIT,
  actively maintained, explicit KDE Plasma 5/6 support) — reused as-is for
  focus tracking. It's a tiny KWin script (`contents/code/main.js`) that
  calls `workspace.windowActivated` and emits a D-Bus signal
  (`scot.massie.FocusNotifier`) with the focused window's pid, process
  name, window class, and caption. Only the KWin script part is needed
  here; `game_lighting.py` listens to the same D-Bus signal directly
  instead of installing FocusNotifier's own separate listener
  service/`activewindow` CLI.
- **[openrgb-keyboard-highlighter](https://github.com/DuckTapeMan35/openrgb-keyboard-highlighter)**
  (GPLv3, active) — not reused directly. It's built around continuously
  tracking which physical keys are *currently held down* (via the `keyboard`
  library, running as a root daemon) to recolor keys in real time, plus
  i3/sway/hyprland workspace integration — solving a different problem than
  ours (per-game *static* per-key layout, triggered by window focus, no
  physical key-hold tracking, no root needed). Its openrgb-python usage
  patterns (per-LED coloring by name) informed `game_lighting.py`.
- `OpenRGB-Monitor-Status` (found in the same search) turned out to be
  Windows-only (PowerShell/VBS) and isn't applicable here.

## What was actually verified (not just read about)

- **SDK connection works against the real running server**, but requires
  `protocol_version=3` explicitly. `openrgb-python` 0.3.6 defaults to
  protocol v4 and sends a plugin-list request on connect; a self-compiled
  OpenRGB 1.0 (see [MysticLight](https://github.com/droltr/MysticLight),
  the sibling project this service was built alongside and depends on for
  the running SDK server) doesn't handle that request and the server closes
  the connection ("recv_select failed receiving magic").
  `game_lighting.py` already passes `protocol_version=3`.
- **Real devices enumerated** through that connection:
  - `MSI MYSTIC LIGHT` — motherboard, 4 zones (JAF, JARGB 1-3)
  - `ENE DRAM` x2 — RAM sticks, 8 LEDs each
  - `SteelSeries Apex Pro` — keyboard, **112 individually addressable
    LEDs**, named `Key: A`, `Key: B`, ... `Key: W`, confirmed directly
    (per-key WASD highlighting is fully supported by this hardware).
  - `SteelSeries Rival 5` — mouse, 10 LEDs.
- **FocusNotifier's KWin script was installed and enabled live** on this
  machine (`kpackagetool6 --type KWin/Script -i .`, then
  `FocusNotifierEnabled=true` in `kwinrc` + `KWin reconfigure` over D-Bus) —
  confirmed as the correct mechanism for KDE Plasma 6.7.4 (the session
  running here). Its JS source explicitly branches on
  `workspace.windowActivated ?? workspace.clientActivated` to support both
  KDE 6 and KDE 5.
- **Focus tracking verified end-to-end, live, with a real game.** While
  Counter-Strike 2 (native Linux build, process `cs2`) was running and the
  user alt-tabbed between it and the desktop several times, the service log
  showed the real sequence: `Game mode ON: cs2 (pname=cs2, class=cs2)` →
  `Game mode OFF` → `Game mode ON: cs2 ...`, matching every switch. The
  keyboard's per-key layout was pushed on each `ON` and the temperature loop
  resumed control on each `OFF`.
- **CPU temperature reading works** (`sensors -j` parsing for
  `k10temp`/`coretemp`/`zenpower`) — the temperature loop ran error-free for
  extended periods driving the motherboard, RAM, keyboard, and mouse.

The service also treats SDK availability as transient. It waits for the
configured minimum device inventory, reconnects with bounded exponential
backoff after a socket or protocol failure, and rebuilds all device references
after reconnecting. Configure `openrgb.required_device_counts` to prevent an
early connection from permanently missing devices that OpenRGB is still
discovering.

Temperature-managed devices are switched to the configured `Direct` mode once
after discovery or reconnection, with `save=False`. This is necessary for DRAM
devices that otherwise remain in a firmware effect such as `Rainbow`, where a
plain SDK color call can be ignored without reporting an error. Devices that
do not advertise the configured mode are skipped with a warning.

At startup, the service performs a best-effort current-window query through
`xdotool` for XWayland applications, then listens to FocusNotifier events.
The desktop layout is applied deterministically when the initial window cannot
be queried. If `dbus-monitor` exits, the focus listener is restarted rather
than silently stopping context updates.

## Bug found and fixed during live testing

The first per-key layout push warned `Unknown keyboard LED name: Key: SPACE`
— the code built LED names as `f"Key: {key_name.upper()}"`, but this
keyboard's actual LED name is `Key: Space` (title case), not `Key: SPACE`.
Single-letter keys (`w` → `W`) happened to work by accident; anything
multi-character (`space`, `left control`, `escape`, ...) didn't. Fixed by
matching LED names case-insensitively instead of guessing a casing
convention. Re-verified live with zero "Unknown keyboard LED name" warnings
across a 24-key CS2 layout.

## Status: installed, running, and actively used

Installed on this machine as a permanent systemd `--user` service:

1. Host venv at `~/.local/share/game-lighting/venv` (no compiled
   dependencies, so a plain host venv is enough — the Toolbox used for
   OpenRGB itself isn't needed here).
2. `~/.config/game-lighting/config.yaml` has real mappings for two games —
   see "Included game layouts" below.
3. Installed as `~/.config/systemd/user/game-lighting.service`
   (`After=`/`Requires=openrgb-server.service`), `systemctl --user enable
   --now`d — active, survives reboot/login.
4. The old standalone `openrgb-thermal-sync.py` +
   `openrgb-thermal-sync.service` (did only the temperature part, for the
   whole system rather than per-device) were removed entirely, superseded
   by this service.

## Included game layouts

`config.example.yaml` ships two real, tested layouts, structured to avoid
repeating shared key colors across games — a YAML anchor
(`_shared.wasd_green`, and for CS2, `_shared.cs2_rest_red`) is defined once
and merged into each game's `keys` map with `<<: *anchor_name` (or
`<<: [*a, *b]` for more than one). Plain PyYAML feature, no code changes
needed to add more games without repeating the movement keys every time.

The example configuration also contains a low-brightness coding layout for
the desktop: navigation and modifier keys are grouped by color while the rest
of the keyboard remains dark blue. Edit `desktop.keyboard` and
`desktop.mouse` without changing the service code.

- **`cs2`** (matches process `cs2`): WASD green, every other key CS2
  actually uses (jump, crouch, walk, weapon slots 1-5, reload, quick-switch,
  drop, use/plant/defuse, buy menu, scoreboard, menu, chat, radio) — all one
  color, red, per the user's request to keep it simple. Mouse also turns
  red in game mode.
- **`factorio`** (matches process `factorio`): WASD green, inventory/pipette/
  rotate/quickbar-1-5/map-toggle in their own colors — covers the most
  commonly used single-key binds; Factorio has more (some are quickbar
  items, some are combos like Shift+R or Ctrl+C that a static per-key color
  can't represent).

## Adding another game

1. Find the process name: run `journalctl --user -u game-lighting.service -f`
   while the game is focused (or bump the logger to DEBUG) — focus-change
   log lines show `pname=... class=...`.
2. Add a new entry under `games:` in `~/.config/game-lighting/config.yaml`,
   reusing `<<: *wasd_green` (or defining your own anchor) for shared keys.
3. Find real LED names for a different keyboard with:
   `python3 -c "from openrgb import OpenRGBClient; c = OpenRGBClient(protocol_version=3); print([l.name for d in c.devices for l in d.leds if d.type.name == 'KEYBOARD'])"`
4. `systemctl --user restart game-lighting.service`.

## Health check

Run `./health-check.sh` to validate the startup chain without printing serials
or raw hardware identifiers. Exit codes are 0 (healthy), 1 (warning), and 2
(error). The report separates OpenRGB service/process/SDK, CPU sensor,
orchestrator, and udev ownership failures.
