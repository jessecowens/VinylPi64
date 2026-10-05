from __future__ import annotations

import io
import math
import time
import wave
from dataclasses import dataclass

try:
    import audioop
except ModuleNotFoundError:  # Python 3.13 uses the audioop-lts dependency.
    import audioop_lts as audioop  # type: ignore[no-redef]


_DBFS_FLOOR = -120.0


@dataclass(frozen=True)
class AudioLevel:
    dbfs: float
    duration_seconds: float


@dataclass(frozen=True)
class PickupUsageConfig:
    enabled: bool = True
    start_threshold_dbfs: float = -48.0
    stop_threshold_dbfs: float = -61.0
    start_confirm_seconds: float = 3.0
    stop_confirm_seconds: float = 30.0

    @staticmethod
    def from_config(cfg: dict) -> "PickupUsageConfig":
        audio = cfg.get("audio") or {}
        raw = audio.get("pickup_usage") or {}

        def number(name: str, default: float, minimum: float, maximum: float) -> float:
            try:
                value = float(raw.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(maximum, value))

        start_threshold = number("start_threshold_dbfs", -48.0, -100.0, -1.0)
        stop_threshold = number("stop_threshold_dbfs", -61.0, -120.0, -1.0)
        # Hysteresis requires the start threshold to be louder (numerically
        # higher) than the stop threshold. Keep at least 1 dB between them.
        if stop_threshold >= start_threshold:
            stop_threshold = max(-120.0, start_threshold - 1.0)

        return PickupUsageConfig(
            enabled=bool(raw.get("enabled", True)),
            start_threshold_dbfs=start_threshold,
            stop_threshold_dbfs=stop_threshold,
            start_confirm_seconds=number("start_confirm_seconds", 3.0, 0.1, 120.0),
            stop_confirm_seconds=number("stop_confirm_seconds", 30.0, 0.1, 600.0),
        )


def measure_wav_level(wav_bytes: bytes) -> AudioLevel | None:
    """Return RMS level in dBFS plus the represented WAV duration.

    The recognizer already records PCM WAV data, so this reuses that sample and
    never opens a second audio stream. Invalid/non-PCM data is ignored rather
    than affecting recognition.
    """
    if not wav_bytes:
        return None

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frame_rate = int(wav_file.getframerate() or 0)
            frame_count = int(wav_file.getnframes() or 0)
            sample_width = int(wav_file.getsampwidth() or 0)
            compression = wav_file.getcomptype()
            if frame_rate <= 0 or frame_count <= 0 or sample_width not in (1, 2, 3, 4):
                return None
            if compression != "NONE":
                return None
            frames = wav_file.readframes(frame_count)
    except (wave.Error, EOFError, OSError):
        return None

    if not frames:
        return None

    try:
        rms = float(audioop.rms(frames, sample_width))
    except (audioop.error, ValueError):
        return None

    peak = float((1 << (8 * sample_width - 1)) - 1)
    if rms <= 0.0 or peak <= 0.0:
        dbfs = _DBFS_FLOOR
    else:
        dbfs = max(_DBFS_FLOOR, 20.0 * math.log10(rms / peak))

    return AudioLevel(
        dbfs=float(dbfs),
        duration_seconds=frame_count / float(frame_rate),
    )


class PickupUsageTracker:
    """Infer stylus/pickup use from USB audio level with hysteresis.

    While active, wall-clock time between successive samples is counted. This
    intentionally includes the recognition/API gap between recordings because
    the stylus is assumed to remain on the record until sustained silence proves
    otherwise. Start confirmation is counted retroactively, and the configured
    stop confirmation/grace period is counted as usage as well.
    """

    def __init__(self, config: PickupUsageConfig | None = None):
        self.config = config or PickupUsageConfig()
        self.active = False
        self._candidate_started_at: float | None = None
        self._below_started_at: float | None = None
        self._accounted_until: float | None = None

    def configure(self, config: PickupUsageConfig) -> None:
        was_enabled = self.config.enabled
        self.config = config
        if was_enabled and not config.enabled:
            self.reset()

    def reset(self) -> None:
        self.active = False
        self._candidate_started_at = None
        self._below_started_at = None
        self._accounted_until = None

    def observe(
        self,
        level_dbfs: float,
        sample_duration_seconds: float,
        *,
        observed_at: float | None = None,
    ) -> float:
        """Consume one measured sample and return newly accrued usage seconds."""
        if not self.config.enabled:
            self.reset()
            return 0.0

        sample_duration = max(0.0, float(sample_duration_seconds))
        sample_end = float(time.monotonic() if observed_at is None else observed_at)
        sample_start = sample_end - sample_duration
        level = float(level_dbfs)

        if not self.active:
            if level >= self.config.start_threshold_dbfs:
                if self._candidate_started_at is None:
                    self._candidate_started_at = sample_start
                if sample_end - self._candidate_started_at >= self.config.start_confirm_seconds:
                    self.active = True
                    self._below_started_at = None
                    start_at = self._candidate_started_at
                    self._candidate_started_at = None
                    self._accounted_until = sample_end
                    return max(0.0, sample_end - start_at)
            else:
                self._candidate_started_at = None
            return 0.0

        accounted_until = self._accounted_until
        if accounted_until is None:
            accounted_until = sample_start

        if level <= self.config.stop_threshold_dbfs:
            if self._below_started_at is None:
                self._below_started_at = sample_start

            stop_at = self._below_started_at + self.config.stop_confirm_seconds
            if sample_end >= stop_at:
                delta = max(0.0, stop_at - accounted_until)
                self.reset()
                return delta
        else:
            # Any level above the stop threshold means the stylus is still
            # plausibly active. A new quiet streak must start from scratch.
            self._below_started_at = None

        delta = max(0.0, sample_end - accounted_until)
        self._accounted_until = sample_end
        return delta
