#!/usr/bin/env python3
"""
Game-aware RGB lighting orchestrator for the MysticLight OpenRGB setup.

Behavior:
  - Motherboard (MSI MYSTIC LIGHT) and RAM (ENE DRAM) LEDs always follow the
    CPU temperature. Game mode never touches them.
  - Keyboard and mouse follow the temperature too, UNLESS a mapped game is
    the currently focused window, in which case the keyboard gets a
    per-key custom layout from the config file (mouse optionally too).

Focus tracking:
  - Listens directly to the D-Bus signal emitted by the "Focus Notifier"
    KWin script (scot.massie.FocusNotifier / MIT license,
    https://github.com/c-massie/FocusNotifier). Only the KWin script itself
    is required to be installed and enabled; this process replaces
    FocusNotifier's own bash listener + `activewindow` CLI, so those do not
    need to be installed.

SDK connection:
  - Talks to the OpenRGB SDK server at 127.0.0.1:6742 as a CLIENT ONLY. It
    never launches OpenRGB. This must be openrgb-server.service (or
    whatever process owns the SDK port) already running.
  - Connects with protocol_version=3 explicitly. Our self-compiled
    OpenRGB 1.0 build does not implement the plugin-list request added in
    SDK protocol v4, and openrgb-python 0.3.6 defaults to v4 and sends
    that request unconditionally on connect, which the server rejects
    ("recv_select failed receiving magic") and closes the socket. Forcing
    v3 skips that request and connects cleanly. This was verified against
    the real running server (see game-lighting/README.md).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
from openrgb import OpenRGBClient
from openrgb.utils import DeviceType, RGBColor

LOG = logging.getLogger("game-lighting")

CONFIG_PATH = Path(
    os.environ.get(
        "GAME_LIGHTING_CONFIG",
        str(Path.home() / ".config" / "game-lighting" / "config.yaml"),
    )
)

DBUS_MATCH = (
    "destination=scot.massie.FocusNotifier,"
    "path=/scot/massie/FocusNotifier,"
    "interface=scot.massie.FocusNotifier"
)


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"Config file not found: {path}\n"
            f"Copy game-lighting/config.example.yaml there and edit it first."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_cpu_temp_celsius() -> Optional[float]:
    """Read CPU package temperature via lm-sensors JSON output."""
    try:
        out = subprocess.run(
            ["sensors", "-j"], capture_output=True, text=True, timeout=2, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    for chip_name, chip in data.items():
        chip_lower = chip_name.lower()
        if "k10temp" not in chip_lower and "coretemp" not in chip_lower and "zenpower" not in chip_lower:
            continue
        for feature_name, feature in chip.items():
            if not isinstance(feature, dict):
                continue
            for key, value in feature.items():
                if key.endswith("_input") and isinstance(value, (int, float)):
                    return float(value)
    return None


def temp_to_color(temp_c: Optional[float], cold: RGBColor, hot: RGBColor,
                   cold_at: float, hot_at: float) -> RGBColor:
    if temp_c is None:
        return cold
    t = max(0.0, min(1.0, (temp_c - cold_at) / max(1.0, (hot_at - cold_at))))
    r = int(cold.red + (hot.red - cold.red) * t)
    g = int(cold.green + (hot.green - cold.green) * t)
    b = int(cold.blue + (hot.blue - cold.blue) * t)
    return RGBColor(r, g, b)


class GameLighting:
    def __init__(self, config: dict):
        self.config = config
        self.client = OpenRGBClient(
            address=config.get("openrgb", {}).get("host", "127.0.0.1"),
            port=config.get("openrgb", {}).get("port", 6742),
            name="game-lighting",
            protocol_version=3,
        )
        self.lock = threading.Lock()
        self.active_game: Optional[str] = None
        self.stop_event = threading.Event()

        self.keyboard = self._find_device(DeviceType.KEYBOARD)
        self.mouse = self._find_device(DeviceType.MOUSE)
        self.motherboard = self._find_device(DeviceType.MOTHERBOARD)
        self.dram = [d for d in self.client.devices if d.type == DeviceType.DRAM]

        LOG.info(
            "Devices: keyboard=%s mouse=%s motherboard=%s dram=%d",
            self.keyboard.name if self.keyboard else None,
            self.mouse.name if self.mouse else None,
            self.motherboard.name if self.motherboard else None,
            len(self.dram),
        )

    def _find_device(self, device_type: DeviceType):
        for d in self.client.devices:
            if d.type == device_type:
                return d
        return None

    # ------------------------------------------------------------------ #
    # Temperature loop: always drives motherboard + RAM; drives keyboard
    # and mouse only while no game is active.
    # ------------------------------------------------------------------ #
    def temperature_loop(self):
        temp_cfg = self.config.get("temperature", {})
        cold = RGBColor(*temp_cfg.get("cold_rgb", [0, 80, 255]))
        hot = RGBColor(*temp_cfg.get("hot_rgb", [255, 30, 0]))
        cold_at = float(temp_cfg.get("cold_at_celsius", 40))
        hot_at = float(temp_cfg.get("hot_at_celsius", 85))
        interval = float(temp_cfg.get("interval_seconds", 1.0))

        while not self.stop_event.is_set():
            temp = read_cpu_temp_celsius()
            color = temp_to_color(temp, cold, hot, cold_at, hot_at)

            if self.motherboard is not None:
                try:
                    self.motherboard.set_color(color)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Failed to set motherboard color: %s", exc)

            for dram_dev in self.dram:
                try:
                    dram_dev.set_color(color)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Failed to set DRAM color: %s", exc)

            with self.lock:
                game_active = self.active_game is not None

            if not game_active:
                if self.keyboard is not None:
                    try:
                        self.keyboard.set_color(color)
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("Failed to set keyboard color: %s", exc)
                if self.mouse is not None:
                    try:
                        self.mouse.set_color(color)
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("Failed to set mouse color: %s", exc)

            self.stop_event.wait(interval)

    # ------------------------------------------------------------------ #
    # Focus tracking: parses the same D-Bus signal FocusNotifier's own
    # listener script parses, without requiring that listener to be
    # installed - only its KWin script needs to be installed and enabled.
    # ------------------------------------------------------------------ #
    def focus_loop(self):
        proc = subprocess.Popen(
            ["dbus-monitor", DBUS_MATCH],
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        pending = {}
        try:
            for line in proc.stdout:
                if self.stop_event.is_set():
                    break
                line = line.strip()
                match = re.match(r"§(\w+):\s*(.*)", line)
                if match:
                    key, value = match.group(1), match.group(2).strip()
                    pending[key] = value
                elif line == "§end":
                    self._on_focus_change(pending)
                    pending = {}
        finally:
            proc.terminate()

    def _on_focus_change(self, info: dict):
        pname = info.get("pname", "") or info.get("wname", "")
        wclass = info.get("wclass", "")
        LOG.debug("Focus changed: pname=%r wclass=%r", pname, wclass)

        games = self.config.get("games", {})
        matched_name = None
        matched_cfg = None
        for name, cfg in games.items():
            patterns = cfg.get("match", [])
            haystacks = [pname.lower(), wclass.lower()]
            if any(pat.lower() in hs for pat in patterns for hs in haystacks):
                matched_name, matched_cfg = name, cfg
                break

        with self.lock:
            previously_active = self.active_game
            self.active_game = matched_name

        if matched_name == previously_active:
            return

        if matched_name is not None:
            LOG.info("Game mode ON: %s (pname=%s, class=%s)", matched_name, pname, wclass)
            self._apply_game_layout(matched_cfg)
        else:
            LOG.info("Game mode OFF (was: %s)", previously_active)
            # Temperature loop will repaint keyboard/mouse on its next tick.

    def _apply_game_layout(self, game_cfg: dict):
        if self.keyboard is None:
            return
        keyboard_layout = game_cfg.get("keyboard", {})
        base_color = RGBColor(*keyboard_layout.get("base_rgb", [0, 0, 0]))
        colors = [base_color] * len(self.keyboard.leds)

        # LED naming case varies by key (e.g. "Key: W" vs "Key: Space"), so
        # match case-insensitively instead of guessing a casing convention.
        led_index_by_name = {
            led.name.lower(): i for i, led in enumerate(self.keyboard.leds)
        }
        for key_name, rgb in keyboard_layout.get("keys", {}).items():
            led_name = f"key: {key_name.lower()}"
            idx = led_index_by_name.get(led_name)
            if idx is None:
                LOG.warning("Unknown keyboard LED name: %s", led_name)
                continue
            colors[idx] = RGBColor(*rgb)

        try:
            self.keyboard.set_colors(colors)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Failed to apply game keyboard layout: %s", exc)

        mouse_cfg = game_cfg.get("mouse")
        if mouse_cfg is not None and self.mouse is not None:
            try:
                self.mouse.set_color(RGBColor(*mouse_cfg.get("rgb", [0, 0, 0])))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Failed to apply game mouse color: %s", exc)

    def run(self):
        threads = [
            threading.Thread(target=self.temperature_loop, name="temperature", daemon=True),
            threading.Thread(target=self.focus_loop, name="focus", daemon=True),
        ]
        for t in threads:
            t.start()

        def handle_signal(signum, frame):  # noqa: ARG001
            LOG.info("Received signal %s, shutting down", signum)
            self.stop_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        while not self.stop_event.is_set():
            time.sleep(0.5)

        for t in threads:
            t.join(timeout=5)
        self.client.disconnect()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config(CONFIG_PATH)
    app = GameLighting(config)
    app.run()


if __name__ == "__main__":
    sys.exit(main())
