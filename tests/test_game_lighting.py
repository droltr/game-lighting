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
    def __init__(self, device_type, name, leds=None):
        self.type = device_type
        self.name = name
        self.leds = leds or []
        self.set_color = MagicMock()
        self.set_colors = MagicMock()


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


if __name__ == "__main__":
    unittest.main()
