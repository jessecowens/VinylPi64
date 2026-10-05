from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class VinylAudioSleepConfig:
    """Configuration for putting Vinyl recognition to sleep from input level.

    The recognizer already owns the USB input while Vinyl mode is active. This
    tracker therefore consumes the dBFS measurement of the same WAV sample and
    never opens a second audio stream.
    """

    enabled: bool = True
    threshold_dbfs: float = -61.0
    confirm_seconds: float = 30.0

    @staticmethod
    def from_config(cfg: dict) -> "VinylAudioSleepConfig":
        behavior = cfg.get("behavior") or {}

        try:
            threshold = float(behavior.get("vinyl_audio_sleep_threshold_dbfs", -61.0))
        except (TypeError, ValueError):
            threshold = -61.0
        threshold = max(-120.0, min(-1.0, threshold))

        try:
            confirm_seconds = float(behavior.get("vinyl_audio_sleep_confirm_seconds", 30.0))
        except (TypeError, ValueError):
            confirm_seconds = 30.0
        confirm_seconds = max(0.1, min(600.0, confirm_seconds))

        return VinylAudioSleepConfig(
            enabled=bool(behavior.get("vinyl_audio_sleep_enabled", True)),
            threshold_dbfs=threshold,
            confirm_seconds=confirm_seconds,
        )


class LowAudioSleepTracker:
    """Trigger once the measured input stays quiet for the configured grace period.

    A sample above the threshold resets the quiet streak. The timer is based on
    monotonic wall-clock time, starting at the beginning of the first quiet
    sample. This intentionally includes the short recognition/API gap between
    samples: once the stylus is lifted, the input is expected to remain quiet
    until a later sample proves otherwise.
    """

    def __init__(self, config: VinylAudioSleepConfig | None = None):
        self.config = config or VinylAudioSleepConfig()
        self._below_started_at: float | None = None

    def configure(self, config: VinylAudioSleepConfig) -> None:
        was_enabled = self.config.enabled
        threshold_changed = config.threshold_dbfs != self.config.threshold_dbfs
        confirm_changed = config.confirm_seconds != self.config.confirm_seconds
        self.config = config
        if (was_enabled and not config.enabled) or threshold_changed or confirm_changed:
            self.reset()

    def reset(self) -> None:
        self._below_started_at = None

    def observe(
        self,
        level_dbfs: float,
        sample_duration_seconds: float,
        *,
        observed_at: float | None = None,
    ) -> bool:
        if not self.config.enabled:
            self.reset()
            return False

        sample_duration = max(0.0, float(sample_duration_seconds))
        sample_end = float(time.monotonic() if observed_at is None else observed_at)
        sample_start = sample_end - sample_duration
        level = float(level_dbfs)

        if level > self.config.threshold_dbfs:
            self._below_started_at = None
            return False

        if self._below_started_at is None:
            self._below_started_at = sample_start

        if sample_end - self._below_started_at >= self.config.confirm_seconds:
            self.reset()
            return True

        return False
