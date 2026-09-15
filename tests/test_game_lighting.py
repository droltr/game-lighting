"""Unit tests for game_lighting.py.

Run with:
    python3 -m unittest discover -s game-lighting/tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import game_lighting  # noqa: E402
from openrgb.utils import DeviceType, RGBColor  # noqa: E402


class FakeLed:
    def __init__(self, name: str):
        self.name = name


class FakeDevice:
    def __init__(self, device_type, name, leds=None, modes=None, active_mode=0):
        self.type = device_type
        self.name = name
        self.leds = leds or []
        self.modes = modes or []
        self.active_mode = active_mode
        self.set_color = MagicMock()
        self.set_colors = MagicMock()
        self.set_mode = MagicMock()


class FakeMode:
    def __init__(self, name: str):
        self.name = name


class TempToColorTests(unittest.TestCase):
    def setUp(self):
        self.cold = RGBColor(0, 80, 255)
        self.hot = RGBColor(255, 30, 0)

    def test_at_or_below_cold_threshold_returns_cold(self):
        color = game_lighting.temp_to_color(30, self.cold, self.hot, cold_at=40, hot_at=85)
        self.assertEqual((color.red, color.green, color.blue), (0, 80, 255))

    def test_at_or_above_hot_threshold_returns_hot(self):
        color = game_lighting.temp_to_color(90, self.cold, self.hot, cold_at=40, hot_at=85)
        self.assertEqual((color.red, color.green, color.blue), (255, 30, 0))

    def test_midpoint_is_interpolated(self):
        color = game_lighting.temp_to_color(62.5, self.cold, self.hot, cold_at=40, hot_at=85)
        self.assertEqual((color.red, color.green, color.blue), (127, 55, 127))

    def test_missing_temperature_falls_back_to_cold(self):
        color = game_lighting.temp_to_color(None, self.cold, self.hot, cold_at=40, hot_at=85)
        self.assertEqual((color.red, color.green, color.blue), (0, 80, 255))


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            game_lighting.load_config(Path("/nonexistent/game-lighting-config.yaml"))

    def test_valid_file_is_parsed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write("openrgb:\n  host: 127.0.0.1\n  port: 6742\n")
            path = Path(handle.name)
        try:
            config = game_lighting.load_config(path)
            self.assertEqual(config["openrgb"]["host"], "127.0.0.1")
            self.assertEqual(config["openrgb"]["port"], 6742)
        finally:
            path.unlink()


class FocusChangeTests(unittest.TestCase):
    """Exercises GameLighting._on_focus_change against a mocked SDK client."""

    def _make_lighting(self):
        config = {
            "openrgb": {"host": "127.0.0.1", "port": 6742},
            "games": {
                "example_fps": {
                    "match": ["cs2"],
                    "keyboard": {
                        "base_rgb": [10, 10, 10],
                        "keys": {"w": [0, 255, 0]},
                    },
                    "mouse": {"rgb": [255, 0, 0]},
                }
            },
        }
        keyboard = FakeDevice(DeviceType.KEYBOARD, "Keyboard", leds=[FakeLed("Key: W")])
        mouse = FakeDevice(DeviceType.MOUSE, "Mouse")

        with patch.object(game_lighting, "OpenRGBClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.devices = [keyboard, mouse]
            mock_client_cls.return_value = mock_client
            lighting = game_lighting.GameLighting(config)
        return lighting, keyboard, mouse

    def test_matching_process_name_activates_game_mode(self):
        lighting, keyboard, mouse = self._make_lighting()
        lighting._on_focus_change({"pname": "cs2.exe", "wclass": ""})

        self.assertEqual(lighting.active_game, "example_fps")
        keyboard.set_colors.assert_called_once()
        mouse.set_color.assert_called_once()

    def test_unmatched_focus_clears_game_mode(self):
        lighting, keyboard, mouse = self._make_lighting()
        lighting._on_focus_change({"pname": "cs2.exe", "wclass": ""})
        lighting._on_focus_change({"pname": "firefox", "wclass": ""})

        self.assertIsNone(lighting.active_game)

    def test_repeated_focus_on_same_game_does_not_reapply(self):
        lighting, keyboard, mouse = self._make_lighting()
        lighting._on_focus_change({"pname": "cs2.exe", "wclass": ""})
        keyboard.set_colors.reset_mock()
        lighting._on_focus_change({"pname": "cs2.exe", "wclass": ""})

        keyboard.set_colors.assert_not_called()

    def test_unknown_keyboard_led_name_is_skipped_without_error(self):
        config = {
            "openrgb": {"host": "127.0.0.1", "port": 6742},
            "games": {
                "example_fps": {
                    "match": ["cs2"],
                    "keyboard": {
                        "base_rgb": [10, 10, 10],
                        "keys": {"nonexistent_key": [0, 255, 0]},
                    },
                }
            },
        }
        keyboard = FakeDevice(DeviceType.KEYBOARD, "Keyboard", leds=[FakeLed("Key: W")])

        with patch.object(game_lighting, "OpenRGBClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.devices = [keyboard]
            mock_client_cls.return_value = mock_client
            lighting = game_lighting.GameLighting(config)

        lighting._on_focus_change({"pname": "cs2.exe", "wclass": ""})
        keyboard.set_colors.assert_called_once()


class ConnectionRecoveryTests(unittest.TestCase):
    def _config(self, **openrgb):
        return {
            "openrgb": {
                "host": "127.0.0.1",
                "port": 6742,
                "discovery_poll_seconds": 0,
                **openrgb,
            },
            "games": {},
        }

    def test_initial_connection_failure_is_retried(self):
        client = MagicMock()
        client.devices = []

        with patch.object(
            game_lighting,
            "OpenRGBClient",
            side_effect=[ConnectionRefusedError(), client],
        ):
            with self.assertLogs("game-lighting", level="WARNING") as logs:
                lighting = game_lighting.GameLighting(self._config())
            self.assertIsNone(lighting.client)
            self.assertIn("ConnectionRefusedError", "\n".join(logs.output))

            self.assertTrue(lighting._ensure_connected())
            self.assertIs(lighting.client, client)

    def test_discovery_waits_for_required_dram(self):
        client = MagicMock()
        keyboard = FakeDevice(DeviceType.KEYBOARD, "Keyboard")
        dram_a = FakeDevice(DeviceType.DRAM, "DRAM A")
        dram_b = FakeDevice(DeviceType.DRAM, "DRAM B")
        client.devices = [keyboard]

        def finish_discovery():
            client.devices = [keyboard, dram_a, dram_b]

        client.update.side_effect = finish_discovery

        with patch.object(game_lighting, "OpenRGBClient", return_value=client):
            lighting = game_lighting.GameLighting(
                self._config(
                    discovery_timeout_seconds=1,
                    required_device_counts={"keyboard": 1, "dram": 2},
                )
            )

        client.update.assert_called_once()
        self.assertEqual(lighting.dram, [dram_a, dram_b])

    def test_write_failure_drops_client_for_reconnection(self):
        client = MagicMock()
        client.devices = []

        with patch.object(game_lighting, "OpenRGBClient", return_value=client):
            lighting = game_lighting.GameLighting(self._config())

        with self.assertLogs("game-lighting", level="WARNING") as logs:
            lighting._drop_connection("temperature update", ConnectionResetError())

        self.assertIsNone(lighting.client)
        client.disconnect.assert_called_once()
        self.assertIn("ConnectionResetError", "\n".join(logs.output))


class TemperatureDeviceModeTests(unittest.TestCase):
    def _lighting(self, device):
        config = {
            "openrgb": {"host": "127.0.0.1", "port": 6742},
            "temperature": {"device_mode": "Direct"},
            "games": {},
        }
        client = MagicMock()
        client.devices = [device]
        with patch.object(game_lighting, "OpenRGBClient", return_value=client):
            return game_lighting.GameLighting(config)

    def test_rainbow_dram_switches_to_direct_without_saving(self):
        dram = FakeDevice(
            DeviceType.DRAM,
            "DRAM",
            modes=[FakeMode("Direct"), FakeMode("Rainbow")],
            active_mode=1,
        )

        lighting = self._lighting(dram)

        dram.set_mode.assert_called_once_with("Direct", save=False)
        self.assertEqual(lighting.temperature_devices, [dram])

    def test_already_direct_device_does_not_switch_mode(self):
        motherboard = FakeDevice(
            DeviceType.MOTHERBOARD,
            "Motherboard",
            modes=[FakeMode("Direct")],
        )

        lighting = self._lighting(motherboard)

        motherboard.set_mode.assert_not_called()
        self.assertEqual(lighting.temperature_devices, [motherboard])

    def test_device_without_direct_mode_is_skipped(self):
        dram = FakeDevice(
            DeviceType.DRAM,
            "DRAM",
            modes=[FakeMode("Rainbow")],
        )

        with self.assertLogs("game-lighting", level="WARNING") as logs:
            lighting = self._lighting(dram)

        self.assertEqual(lighting.temperature_devices, [])
        self.assertIn("mode 'Direct' is unavailable", "\n".join(logs.output))


class ContextProfileTests(unittest.TestCase):
    def _make_lighting(self):
        config = {
            "openrgb": {"host": "127.0.0.1", "port": 6742},
            "temperature": {"cold_rgb": [1, 2, 3], "hot_rgb": [255, 0, 0]},
            "transition": {"duration_seconds": 0},
            "desktop": {
                "keyboard": {
                    "base_rgb": [8, 12, 24],
                    "keys": {"enter": [0, 220, 140]},
                },
                "mouse": {"rgb": [20, 80, 140]},
            },
            "games": {
                "example_fps": {
                    "match": ["cs2"],
                    "keyboard": {
                        "base_rgb": [10, 10, 10],
                        "keys": {"w": [0, 255, 0]},
                    },
                }
            },
        }
        keyboard = FakeDevice(
            DeviceType.KEYBOARD,
            "Keyboard",
            leds=[FakeLed("Key: W"), FakeLed("Key: Enter")],
        )
        mouse = FakeDevice(DeviceType.MOUSE, "Mouse")
        client = MagicMock()
        client.devices = [keyboard, mouse]
        with patch.object(game_lighting, "OpenRGBClient", return_value=client):
            lighting = game_lighting.GameLighting(config)
        return lighting, keyboard, mouse

    @patch.object(game_lighting, "read_cpu_temp_celsius", return_value=40)
    def test_initial_desktop_focus_applies_transition_then_coding_layout(self, _temp):
        lighting, keyboard, mouse = self._make_lighting()

        lighting._on_focus_change({})

        self.assertTrue(lighting.context_initialized)
        self.assertIsNone(lighting.active_game)
        keyboard.set_color.assert_called_once()
        keyboard.set_colors.assert_called_once()
        self.assertEqual(mouse.set_color.call_count, 2)

    @patch.object(game_lighting, "read_cpu_temp_celsius", return_value=40)
    def test_game_exit_returns_to_desktop_layout(self, _temp):
        lighting, keyboard, _mouse = self._make_lighting()
        lighting._on_focus_change({"pname": "cs2", "wclass": "cs2"})
        keyboard.set_colors.reset_mock()

        lighting._on_focus_change({"pname": "code", "wclass": "code"})

        self.assertIsNone(lighting.active_game)
        keyboard.set_colors.assert_called_once()

    @patch.object(game_lighting.subprocess, "run")
    def test_initial_focus_reads_xwayland_process_and_class(self, run):
        run.side_effect = [
            MagicMock(stdout="100\n"),
            MagicMock(stdout="4242\n"),
            MagicMock(stdout="cs2\n"),
            MagicMock(stdout="cs2\n"),
        ]
        lighting, _keyboard, _mouse = self._make_lighting()

        self.assertEqual(
            lighting._read_initial_focus(),
            {"pname": "cs2", "wclass": "cs2"},
        )


if __name__ == "__main__":
    unittest.main()
