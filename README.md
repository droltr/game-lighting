# Game-aware RGB lighting

Keyboard and mouse follow CPU temperature by default. When a mapped game is
the focused window, the keyboard switches to a per-key custom layout for
that game (e.g. WASD highlighted) instead. Motherboard (MSI MYSTIC LIGHT)
and RAM (ENE DRAM) always follow temperature only and are never touched by
game mode.

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
- **Not yet verified**: an actual live focus-change D-Bus signal firing
  end-to-end while `game_lighting.py` listens (that needs a window to
  actually be alt-tabbed to while the script runs interactively — a
  one-time manual check, see "Next steps").

## What's stubbed / not done yet

- `game_lighting.py` is a working, complete first draft, but has not been
  run end-to-end yet (no `~/.config/game-lighting/config.yaml` exists,
  nothing has been installed permanently - see below for why).
- No installation was performed under `$HOME` (no venv, no
  `~/.config/systemd/user/game-lighting.service`, FocusNotifier's own
  listener/`activewindow` CLI was intentionally not installed since it's
  not needed). Persistent installs of this kind were blocked by the
  session's permission classifier when run from this research pass
  ("Unauthorized Persistence") - installing a new autostart service is a
  standing-permission decision the interactive session/user should make,
  not something to do unattended from a background research task. The KWin
  script install (a reversible System Settings-equivalent toggle) went
  through fine; copying files into `~/.local/bin` and a new systemd unit
  did not.
- CPU temperature reading uses `sensors -j` (lm-sensors) parsing for
  `k10temp`/`coretemp`/`zenpower` - not yet tested against this machine's
  actual `sensors` output; the reference script
  (`~/.local/bin/openrgb-thermal-sync.py`, currently disabled) already
  proved a working temperature-reading approach on this exact host and is
  worth diffing against before first run.

## Status: installed and running (temperature mode only)

Done, on this machine:

1. Host venv at `~/.local/share/game-lighting/venv` with `openrgb-python`
   and `pyyaml` installed (no compiled dependencies, so a plain host venv
   was used instead of the Toolbox).
2. `game_lighting.py` copied to `~/.local/share/game-lighting/`.
3. `~/.config/game-lighting/config.yaml` created from
   `config.example.yaml` (still has the placeholder `example_fps` entry —
   not a real mapping yet, see open questions).
4. Verified live against the real running `openrgb-server.service`: SDK
   connects, all four device types are found (`keyboard=SteelSeries Apex
   Pro mouse=SteelSeries Rival 5 motherboard=MSI MYSTIC LIGHT dram=2`), and
   the temperature loop ran error-free driving real hardware.
5. Installed as `~/.config/systemd/user/game-lighting.service`
   (`After=`/`Requires=openrgb-server.service`), `systemctl --user enable
   --now`d — active and will survive reboot/login.
6. The old `~/.local/bin/openrgb-thermal-sync.py` +
   `openrgb-thermal-sync.service` (superseded duplicate) have been removed
   entirely, not just disabled.

**Not yet verified:** the focus-tracking path end-to-end (FocusNotifier
D-Bus signal → `Game mode ON: ...` in the log → keyboard actually changes)
and mouse-in-game-mode, because no real game is mapped in `config.yaml` yet
— this needs real process names/window classes and colors, which only the
user can provide.

## Open questions for the user

- Which games/processes to map first, and their exact process name or
  window class (can be read live from the log — run
  `journalctl --user -u game-lighting.service -f` while the game is
  focused, or temporarily set the logger to DEBUG to see every focus
  change with `pname`/`wclass`).
- Exact highlight colors per game/key (the placeholder config uses green
  WASD + yellow space).
- Confirmed: mouse also switches in game mode (user chose this) — each
  game's `mouse:` block in the config controls it per game.
