from __future__ import annotations

import unittest

from vinylpi.core.audio_standby import LowAudioSleepTracker, VinylAudioSleepConfig


class VinylAudioSleepConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = VinylAudioSleepConfig.from_config({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.threshold_dbfs, -61.0)
        self.assertEqual(cfg.confirm_seconds, 30.0)

    def test_values_are_clamped(self):
        cfg = VinylAudioSleepConfig.from_config({
            "behavior": {
                "vinyl_audio_sleep_enabled": False,
                "vinyl_audio_sleep_threshold_dbfs": -500,
                "vinyl_audio_sleep_confirm_seconds": 0,
            }
        })
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.threshold_dbfs, -120.0)
        self.assertEqual(cfg.confirm_seconds, 0.1)


class LowAudioSleepTrackerTests(unittest.TestCase):
    def test_sustained_quiet_triggers_sleep(self):
        tracker = LowAudioSleepTracker(
            VinylAudioSleepConfig(enabled=True, threshold_dbfs=-61, confirm_seconds=30)
        )

        self.assertFalse(tracker.observe(-65, 3, observed_at=3))
        self.assertFalse(tracker.observe(-65, 3, observed_at=20))
        self.assertTrue(tracker.observe(-65, 3, observed_at=33))

    def test_louder_sample_resets_quiet_streak(self):
        tracker = LowAudioSleepTracker(
            VinylAudioSleepConfig(enabled=True, threshold_dbfs=-61, confirm_seconds=10)
        )

        self.assertFalse(tracker.observe(-65, 3, observed_at=3))
        self.assertFalse(tracker.observe(-40, 3, observed_at=8))
        self.assertFalse(tracker.observe(-65, 3, observed_at=13))
        self.assertFalse(tracker.observe(-65, 3, observed_at=19))
        self.assertTrue(tracker.observe(-65, 3, observed_at=23))

    def test_disabled_tracker_never_triggers(self):
        tracker = LowAudioSleepTracker(
            VinylAudioSleepConfig(enabled=False, threshold_dbfs=-61, confirm_seconds=1)
        )
        self.assertFalse(tracker.observe(-90, 10, observed_at=10))


if __name__ == "__main__":
    unittest.main()
