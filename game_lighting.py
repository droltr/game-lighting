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
        self.lock = threading.Lock()
        self.client_lock = threading.RLock()
        self.active_game: Optional[str] = None
        self.context_initialized = False
        self.stop_event = threading.Event()
        self.client = None
        self.keyboard = None
        self.mouse = None
        self.motherboard = None
        self.dram = []
        self.temperature_devices = []
        self.context_needs_apply = False

        self._ensure_connected()

    def _required_device_counts(self) -> dict[DeviceType, int]:
        configured = self.config.get("openrgb", {}).get("required_device_counts", {})
        names = {
            "keyboard": DeviceType.KEYBOARD,
            "mouse": DeviceType.MOUSE,
            "motherboard": DeviceType.MOTHERBOARD,
            "dram": DeviceType.DRAM,
        }
        return {
            names[name]: int(count)
            for name, count in configured.items()
            if name in names and int(count) > 0
        }

    def _inventory_is_ready(self) -> bool:
        required = self._required_device_counts()
        return all(
            sum(
                device is not None and device.type == device_type
                for device in self.client.devices
            ) >= count
            for device_type, count in required.items()
        )

    def _refresh_devices(self):
        self.keyboard = self._find_device(DeviceType.KEYBOARD)
        self.mouse = self._find_device(DeviceType.MOUSE)
        self.motherboard = self._find_device(DeviceType.MOTHERBOARD)
        self.dram = [
            d for d in self.client.devices
            if d is not None and d.type == DeviceType.DRAM
        ]

    def _connect_once(self):
        openrgb_cfg = self.config.get("openrgb", {})
        client = OpenRGBClient(
            address=openrgb_cfg.get("host", "127.0.0.1"),
            port=openrgb_cfg.get("port", 6742),
            name="game-lighting",
            protocol_version=3,
        )

        timeout = float(openrgb_cfg.get("discovery_timeout_seconds", 20.0))
        poll = float(openrgb_cfg.get("discovery_poll_seconds", 0.5))
        deadline = time.monotonic() + timeout

        try:
            self.client = client
            self._refresh_devices()
            while not self._inventory_is_ready():
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    counts = {
                        device_type.name.lower(): sum(
                            device is not None and device.type == device_type
                            for device in client.devices
                        )
                        for device_type in self._required_device_counts()
                    }
                    raise RuntimeError(
                        f"device discovery timed out; observed counts={counts}"
                    )
                self.stop_event.wait(poll)
                client.update()
                self._refresh_devices()
        except Exception:
            self.client = None
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise

        LOG.info(
            "Devices: keyboard=%s mouse=%s motherboard=%s dram=%d",
            self.keyboard.name if self.keyboard else None,
            self.mouse.name if self.mouse else None,
            self.motherboard.name if self.motherboard else None,
            len(self.dram),
        )
        self._prepare_temperature_devices()
        self.context_needs_apply = True

    def _prepare_temperature_devices(self):
        devices = [d for d in [self.motherboard, *self.dram] if d is not None]
        desired_mode = self.config.get("temperature", {}).get("device_mode")
        self.temperature_devices = []

        for device in devices:
            if not desired_mode:
                self.temperature_devices.append(device)
                continue

            matching_mode = next(
                (mode for mode in device.modes if mode.name.lower() == desired_mode.lower()),
                None,
            )
            if matching_mode is None:
                LOG.warning(
                    "Skipping temperature control for %s: mode %r is unavailable",
                    device.name,
                    desired_mode,
                )
                continue

            active_mode = device.modes[device.active_mode]
            if active_mode.name.lower() != matching_mode.name.lower():
                LOG.info(
                    "Switching %s from %s to %s mode for temperature control",
                    device.name,
                    active_mode.name,
                    matching_mode.name,
                )
                device.set_mode(matching_mode.name, save=False)
            self.temperature_devices.append(device)

    @staticmethod
    def _error_text(exc: Exception) -> str:
        detail = str(exc).strip()
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__

    def _ensure_connected(self) -> bool:
        with self.client_lock:
            if self.client is not None:
                return True
            try:
                self._connect_once()
                return True
            except Exception as exc:  # noqa: BLE001
                LOG.warning("OpenRGB connection unavailable: %s", self._error_text(exc))
                return False

    def _drop_connection(self, operation: str, exc: Exception):
        with self.client_lock:
            LOG.warning(
                "OpenRGB %s failed; reconnecting: %s",
                operation,
                self._error_text(exc),
            )
            client = self.client
            self.client = None
            self.keyboard = None
            self.mouse = None
            self.motherboard = None
            self.dram = []
            self.temperature_devices = []
            if client is not None:
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    def _find_device(self, device_type: DeviceType):
        if self.client is None:
            return None
        for d in self.client.devices:
            if d is not None and d.type == device_type:
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
        openrgb_cfg = self.config.get("openrgb", {})
        retry_initial = float(openrgb_cfg.get("retry_initial_seconds", 1.0))
        retry_max = float(openrgb_cfg.get("retry_max_seconds", 30.0))
        retry = retry_initial

        while not self.stop_event.is_set():
            if not self._ensure_connected():
                self.stop_event.wait(retry)
                retry = min(retry_max, retry * 2)
                continue
            retry = retry_initial

            temp = read_cpu_temp_celsius()
            color = temp_to_color(temp, cold, hot, cold_at, hot_at)

            try:
                with self.client_lock:
                    for device in self.temperature_devices:
                        device.set_color(color)

                    with self.lock:
                        game_active = self.active_game is not None
                        active_game = self.active_game
                    desktop_managed = bool(self.config.get("desktop"))

                    if game_active and self.context_needs_apply:
                        game_cfg = self.config.get("games", {}).get(active_game)
                        if game_cfg is not None:
                            self._apply_context_layout(game_cfg, transition=False)
                            self.context_needs_apply = self.client is None
                    elif desktop_managed and self.context_needs_apply:
                        self._apply_context_layout(
                            self.config.get("desktop", {}), transition=False
                        )
                        self.context_needs_apply = self.client is None
                    elif not game_active and not desktop_managed:
                        if self.keyboard is not None:
                            self.keyboard.set_color(color)
                        if self.mouse is not None:
                            self.mouse.set_color(color)
                        self.context_needs_apply = False
            except Exception as exc:  # noqa: BLE001
                self._drop_connection("temperature update", exc)

            self.stop_event.wait(interval)

    # ------------------------------------------------------------------ #
    # Focus tracking: parses the same D-Bus signal FocusNotifier's own
    # listener script parses, without requiring that listener to be
    # installed - only its KWin script needs to be installed and enabled.
    # ------------------------------------------------------------------ #
    def _read_initial_focus(self) -> dict:
        """Best-effort initial focus query for XWayland windows on KDE."""
        try:
            window_id = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout.strip()
            pid = subprocess.run(
                ["xdotool", "getwindowpid", window_id],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout.strip()
            wclass = subprocess.run(
                ["xdotool", "getwindowclassname", window_id],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout.strip()
            pname = subprocess.run(
                ["ps", "-p", pid, "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout.strip()
            return {"pname": pname, "wclass": wclass}
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return {}

    def focus_loop(self):
        retry = 1.0
        while not self.stop_event.is_set():
            proc = None
            try:
                proc = subprocess.Popen(
                    ["dbus-monitor", DBUS_MATCH],
                    stdout=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                self._on_focus_change(self._read_initial_focus())
                pending = {}
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
                returncode = proc.poll()
                shutting_down = returncode in (-signal.SIGTERM, -signal.SIGKILL)
                if not self.stop_event.is_set() and not shutting_down:
                    LOG.warning("Focus monitor exited; restarting")
            except OSError as exc:
                LOG.warning("Focus monitor unavailable: %s", self._error_text(exc))
            finally:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self.stop_event.wait(retry)

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

        if matched_name == previously_active and self.context_initialized:
            return

        self.context_initialized = True

        if matched_name is not None:
            LOG.info("Game mode ON: %s (pname=%s, class=%s)", matched_name, pname, wclass)
            self._apply_game_layout(matched_cfg)
        else:
            LOG.info("Game mode OFF (was: %s)", previously_active)
            desktop_cfg = self.config.get("desktop")
            if desktop_cfg:
                self._apply_context_layout(desktop_cfg)
            # Without a desktop profile, the temperature loop retains the
            # original behavior and repaints keyboard/mouse on its next tick.

    def _temperature_color(self) -> RGBColor:
        temp_cfg = self.config.get("temperature", {})
        return temp_to_color(
            read_cpu_temp_celsius(),
            RGBColor(*temp_cfg.get("cold_rgb", [0, 80, 255])),
            RGBColor(*temp_cfg.get("hot_rgb", [255, 30, 0])),
            float(temp_cfg.get("cold_at_celsius", 40)),
            float(temp_cfg.get("hot_at_celsius", 85)),
        )

    def _apply_context_layout(self, context_cfg: dict, transition: bool = True):
        if not self._ensure_connected():
            self.context_needs_apply = True
            return

        try:
            with self.client_lock:
                transition_cfg = self.config.get("transition")
                if transition and transition_cfg:
                    color = self._temperature_color()
                    if self.keyboard is not None:
                        self.keyboard.set_color(color)
                    if self.mouse is not None:
                        self.mouse.set_color(color)
                    delay = float(transition_cfg.get("duration_seconds", 0.2))
                    self.stop_event.wait(max(0.0, delay))

                keyboard_layout = context_cfg.get("keyboard", {})
                if self.keyboard is not None and keyboard_layout:
                    base_color = RGBColor(*keyboard_layout.get("base_rgb", [0, 0, 0]))
                    colors = [base_color] * len(self.keyboard.leds)
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
                    self.keyboard.set_colors(colors)

                mouse_cfg = context_cfg.get("mouse")
                if mouse_cfg is not None and self.mouse is not None:
                    self.mouse.set_color(RGBColor(*mouse_cfg.get("rgb", [0, 0, 0])))
                self.context_needs_apply = False
        except Exception as exc:  # noqa: BLE001
            self.context_needs_apply = True
            self._drop_connection("context layout update", exc)

    def _apply_game_layout(self, game_cfg: dict):
        self._apply_context_layout(game_cfg)

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
        if self.client is not None:
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
