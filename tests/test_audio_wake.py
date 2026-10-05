from __future__ import annotations

import math
import struct
import unittest

from vinylpi.core.audio_wake import (
    VinylAutoWakeConfig,
    WakeThresholdTracker,
    pcm16_dbfs,
)


class VinylAutoWakeConfigTests(unittest.TestCase):
    def test_defaults_use_pickup_threshold_when_wake_threshold_is_missing(self):
        cfg = VinylAutoWakeConfig.from_config({
            "audio": {"pickup_usage": {"start_threshold_dbfs": -46.5}},
            "behavior": {"vinyl_auto_wake_enabled": True},
        })

        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.threshold_dbfs, -46.5)
        self.assertEqual(cfg.confirm_seconds, 2.0)
        self.assertEqual(cfg.activity_threshold_dbfs, -46.5)

    def test_values_are_clamped(self):
        cfg = VinylAutoWakeConfig.from_config({
            "behavior": {
                "vinyl_auto_wake_enabled": False,
                "vinyl_auto_wake_threshold_dbfs": -200,
                "vinyl_auto_wake_confirm_seconds": 0,
            }
        })

        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.threshold_dbfs, -100.0)
        self.assertEqual(cfg.confirm_seconds, 0.1)


class WakeThresholdTrackerTests(unittest.TestCase):
    def test_initial_monitor_wakes_after_sustained_level(self):
        tracker = WakeThresholdTracker(
            VinylAutoWakeConfig(enabled=True, threshold_dbfs=-48, confirm_seconds=2),
            require_rearm=False,
        )

        self.assertFalse(tracker.observe(-65, 1.0))
        self.assertFalse(tracker.observe(-40, 1.0))
        self.assertTrue(tracker.observe(-39, 1.0))

    def test_post_sleep_monitor_requires_quiet_rearm(self):
        tracker = WakeThresholdTracker(
            VinylAutoWakeConfig(enabled=True, threshold_dbfs=-48, confirm_seconds=1),
            require_rearm=True,
        )

        # Persistent audio that caused the previous recognition cycle must not
        # immediately wake Shazam again.
        self.assertFalse(tracker.observe(-40, 5.0))
        self.assertFalse(tracker.armed)

        # At least 3 dB below the wake threshold for one second re-arms it.
        self.assertFalse(tracker.observe(-55, 1.0))
        self.assertTrue(tracker.armed)
        self.assertTrue(tracker.observe(-35, 1.0))


    def test_low_turntable_threshold_can_still_rewake_on_pickup_activity(self):
        tracker = WakeThresholdTracker(
            VinylAutoWakeConfig(
                enabled=True,
                threshold_dbfs=-75,
                confirm_seconds=2,
                activity_threshold_dbfs=-48,
            ),
            require_rearm=True,
        )

        # Powered-on idle audio stays above -75 dBFS, so the normal threshold
        # is intentionally blocked after sleep.
        self.assertFalse(tracker.observe(-65, 5.0))
        self.assertFalse(tracker.armed)

        # Lowering the stylus produces the calibrated pickup-level signal and
        # may resume recognition without first powering the turntable off.
        self.assertFalse(tracker.observe(-35, 1.0))
        self.assertTrue(tracker.observe(-32, 1.0))

    def test_short_peak_does_not_trigger(self):
        tracker = WakeThresholdTracker(
            VinylAutoWakeConfig(enabled=True, threshold_dbfs=-48, confirm_seconds=2),
        )

        self.assertFalse(tracker.observe(-35, 0.5))
        self.assertFalse(tracker.observe(-60, 0.5))
        self.assertFalse(tracker.observe(-35, 1.0))


class PcmLevelTests(unittest.TestCase):
    def test_pcm16_dbfs_matches_expected_rms(self):
        amplitude = 3276
        raw = b"".join(struct.pack("<h", value) for value in [amplitude, -amplitude] * 100)
        expected = 20 * math.log10(amplitude / 32767.0)

        self.assertAlmostEqual(pcm16_dbfs(raw), expected, delta=0.2)

    def test_pcm16_silence_uses_floor(self):
        self.assertEqual(pcm16_dbfs(b"\x00\x00" * 100), -120.0)


if __name__ == "__main__":
    unittest.main()
