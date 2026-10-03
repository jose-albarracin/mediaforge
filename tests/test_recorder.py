"""Tests for the protection check in recorder.assess.

Run: .venv/bin/python -m unittest tests/test_recorder.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder import assess  # noqa: E402


def frames(dark: float, diffs: list[float]) -> list[dict]:
    return [{"t": float(i), "dark": dark, "diff": d, "saved": False}
            for i, d in enumerate(diffs)]


def levels(rms: list[float]) -> list[dict]:
    return [{"t": float(i + 1), "rms": r} for i, r in enumerate(rms)]


PLAYING = [0.12, 0.15, 0.09, 0.14, 0.11, 0.13, 0.10, 0.16]
SILENT = [0.0] * 8
MOVING = [255, 3.1, 0.9, 4.2, 1.5, 2.8, 0.7, 3.3]
STILL = [255, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class AssessTest(unittest.TestCase):
    def test_picture_and_sound(self):
        v = assess(frames(0.0, MOVING), levels(PLAYING))
        self.assertEqual(v.kind, "full")
        self.assertTrue(v.video_ok and v.audio_ok)

    def test_black_picture_with_sound_is_audio_only(self):
        v = assess(frames(1.0, STILL), levels(PLAYING))
        self.assertEqual(v.kind, "audio_only")
        self.assertFalse(v.video_ok)
        self.assertTrue(v.audio_ok)

    def test_black_and_silent_is_blocked(self):
        self.assertEqual(assess(frames(1.0, STILL), levels(SILENT)).kind, "blocked")

    def test_moving_picture_without_sound(self):
        self.assertEqual(assess(frames(0.0, MOVING), levels(SILENT)).kind, "video_only")

    def test_still_and_silent_is_paused(self):
        self.assertEqual(assess(frames(0.0, STILL), levels(SILENT)).kind, "paused")

    def test_a_single_sound_blip_is_not_sound(self):
        blip = [0.0, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.assertEqual(assess(frames(1.0, STILL), levels(blip)).kind, "blocked")

    def test_quiet_speaker_still_counts(self):
        quiet = [0.006, 0.004, 0.0, 0.005, 0.007, 0.0, 0.004, 0.006]
        self.assertEqual(assess(frames(0.0, MOVING), levels(quiet)).kind, "full")

    def test_too_short(self):
        self.assertEqual(assess(frames(0.0, [255]), levels([0.1])).kind, "unknown")


if __name__ == "__main__":
    unittest.main()
