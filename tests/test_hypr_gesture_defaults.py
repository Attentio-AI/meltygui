"""Headless regression checks; python -m unittest discover -s tests -p test_hypr_gesture_defaults.py."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import meltygui.core.input.hypr_left_drag as gestures


class GestureDefaults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cls = "arbitrary.pip-app"
        self.enabled = True
        self.synced = True
        self.calls = []
        script = Path(self.temp.name) / "left-drag-toggle"
        script.touch()
        for context in (
            patch.dict(os.environ, XDG_STATE_HOME=self.temp.name),
            patch.object(gestures, "TOGGLE_SCRIPT", str(script)),
            patch.object(gestures, "detect_gestures", side_effect=self.detect),
            patch.object(gestures, "_getoption", return_value=("bool", True)),
        ):
            context.start()
            self.addCleanup(context.stop)

    def detect(self):
        return True, self.enabled, self.cls, self.synced

    def run_helper(self, command, **kwargs):
        self.calls.append(command)
        self.enabled = False
        self.synced = True
        return SimpleNamespace(returncode=0)

    def test_new_app_off_and_later_on_choice_survives(self):
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertFalse(self.enabled)
        self.assertEqual(self.calls[0][1:], ["--gestures", self.cls])
        self.enabled = True  # user turns it on in hyprbars
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertTrue(self.enabled)
        self.assertEqual(len(self.calls), 1)

    def test_existing_off_syncs_resize_without_turning_move_on(self):
        self.enabled = False
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertEqual(self.calls[0][1:], ["--gestures", "--sync", self.cls])

    def test_failed_helper_retries(self):
        gestures._initialize_default(self.cls, lambda *a, **k: SimpleNamespace(returncode=1))
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertFalse(self.enabled)
        self.assertEqual(len(self.calls), 1)

    def test_success_without_effect_is_not_remembered(self):
        gestures._initialize_default(self.cls, lambda *a, **k: SimpleNamespace(returncode=0))
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertFalse(self.enabled)

    def test_global_disable_defers_initialization(self):
        with patch.object(gestures, "_getoption", return_value=("bool", False)):
            gestures._initialize_default(self.cls, self.run_helper)
        self.assertEqual(self.calls, [])
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertFalse(self.enabled)

    def test_wrong_class_is_not_modified(self):
        gestures._initialize_default("guessed-class", self.run_helper)
        self.assertEqual(self.calls, [])

    def test_missing_helper_is_not_remembered(self):
        with patch.object(gestures, "TOGGLE_SCRIPT", self.temp.name + "/missing"):
            gestures._initialize_default(self.cls, self.run_helper)
        gestures._initialize_default(self.cls, self.run_helper)
        self.assertFalse(self.enabled)
