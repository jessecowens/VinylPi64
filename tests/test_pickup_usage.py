from __future__ import annotations

import io
import math
import struct
import unittest
import wave

from vinylpi.core.pickup_usage import (
    PickupUsageConfig,
    PickupUsageTracker,
    measure_wav_level,
)


def make_pcm16_wav(samples: list[int], *, sample_rate: int = 1000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()


class PickupUsageTests(unittest.TestCase):
    def test_measure_wav_level_reports_duration_and_dbfs(self):
        amplitude = 3276  # roughly -20 dBFS
        wav_bytes = make_pcm16_wav([amplitude, -amplitude] * 1000, sample_rate=1000)

        level = measure_wav_level(wav_bytes)

        self.assertIsNotNone(level)
        assert level is not None
        self.assertAlmostEqual(level.duration_seconds, 2.0, places=3)
        self.assertAlmostEqual(level.dbfs, 20 * math.log10(amplitude / 32767), delta=0.2)

    def test_silence_uses_dbfs_floor(self):
        level = measure_wav_level(make_pcm16_wav([0] * 1000, sample_rate=1000))

        self.assertIsNotNone(level)
        assert level is not None
        self.assertEqual(level.dbfs, -120.0)

    def test_tracker_uses_hysteresis_and_stop_grace(self):
        cfg = PickupUsageConfig(
            enabled=True,
            start_threshold_dbfs=-40,
            stop_threshold_dbfs=-50,
            start_confirm_seconds=3,
            stop_confirm_seconds=5,
        )
        tracker = PickupUsageTracker(cfg)

        # Four seconds above the start threshold activates the pickup and the
        # confirmation period is counted retroactively.
        self.assertEqual(tracker.observe(-20, 4, observed_at=4), 4)
        self.assertTrue(tracker.active)

        # A level between the two thresholds remains active because of hysteresis.
        self.assertEqual(tracker.observe(-45, 4, observed_at=10), 6)
        self.assertTrue(tracker.active)

        # Quiet starts at t=10. Five seconds of grace are still counted, then
        # the tracker stops exactly at t=15 rather than at the later observation.
        self.assertEqual(tracker.observe(-70, 4, observed_at=14), 4)
        self.assertEqual(tracker.observe(-70, 4, observed_at=18), 1)
        self.assertFalse(tracker.active)

    def test_tracker_requires_sustained_start_signal(self):
        cfg = PickupUsageConfig(
            enabled=True,
            start_threshold_dbfs=-40,
            stop_threshold_dbfs=-50,
            start_confirm_seconds=5,
            stop_confirm_seconds=10,
        )
        tracker = PickupUsageTracker(cfg)

        self.assertEqual(tracker.observe(-20, 2, observed_at=2), 0)
        self.assertFalse(tracker.active)
        self.assertEqual(tracker.observe(-20, 2, observed_at=5), 5)
        self.assertTrue(tracker.active)

    def test_invalid_threshold_order_is_normalized(self):
        cfg = PickupUsageConfig.from_config({
            "audio": {
                "pickup_usage": {
                    "start_threshold_dbfs": -60,
                    "stop_threshold_dbfs": -40,
                }
            }
        })

        self.assertGreater(cfg.start_threshold_dbfs, cfg.stop_threshold_dbfs)


if __name__ == "__main__":
    unittest.main()
