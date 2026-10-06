from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable


try:
    import audioop
except ModuleNotFoundError:  # Python 3.13 uses the audioop-lts dependency.
    import audioop_lts as audioop  # type: ignore[no-redef]

from vinylpi.config.runtime import read_config

_DBFS_FLOOR = -120.0
_MONITOR_BLOCK_SECONDS = 0.5
_REARM_MARGIN_DB = 3.0
_REARM_CONFIRM_SECONDS = 1.0
_RETRY_SECONDS = 5.0
_DEBUG_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class VinylAutoWakeConfig:
    enabled: bool = True
    threshold_dbfs: float = -48.0
    confirm_seconds: float = 2.0
    activity_threshold_dbfs: float = -48.0

    @staticmethod
    def from_config(cfg: dict) -> "VinylAutoWakeConfig":
        behavior = cfg.get("behavior") or {}
        pickup_usage = (cfg.get("audio") or {}).get("pickup_usage") or {}

        try:
            fallback_threshold = float(pickup_usage.get("start_threshold_dbfs", -48.0))
        except (TypeError, ValueError):
            fallback_threshold = -48.0

        try:
            threshold = float(behavior.get("vinyl_auto_wake_threshold_dbfs", fallback_threshold))
        except (TypeError, ValueError):
            threshold = fallback_threshold
        threshold = max(-100.0, min(-1.0, threshold))

        try:
            confirm_seconds = float(behavior.get("vinyl_auto_wake_confirm_seconds", 2.0))
        except (TypeError, ValueError):
            confirm_seconds = 2.0
        confirm_seconds = max(0.1, min(30.0, confirm_seconds))

        return VinylAutoWakeConfig(
            enabled=bool(behavior.get("vinyl_auto_wake_enabled", True)),
            threshold_dbfs=threshold,
            confirm_seconds=confirm_seconds,
            # If the wake threshold is deliberately low enough to react to the
            # powered-on turntable (for example around -75 dBFS), this louder
            # pickup threshold still lets a newly lowered stylus wake Shazam
            # after auto-sleep without requiring a power cycle first.
            activity_threshold_dbfs=max(threshold, max(-100.0, min(-1.0, fallback_threshold))),
        )


@dataclass(frozen=True)
class AudioWakeEvent:
    level_dbfs: float
    threshold_dbfs: float


class WakeThresholdTracker:
    """Detect a sustained audio level with re-arm protection after auto sleep.

    An initial Vinyl start may wake from an already-active signal. After an
    automatic sleep, ``require_rearm=True`` prevents the same persistent signal
    from immediately waking Shazam again. The input must first fall a few dB
    below the configured wake threshold before another rising transition can
    trigger recognition.
    """

    def __init__(self, config: VinylAutoWakeConfig, *, require_rearm: bool = False):
        self.config = config
        self.require_rearm = bool(require_rearm)
        self.armed = not self.require_rearm
        self._above_seconds = 0.0
        self._below_seconds = 0.0

    def configure(self, config: VinylAutoWakeConfig) -> None:
        threshold_changed = config.threshold_dbfs != self.config.threshold_dbfs
        confirm_changed = config.confirm_seconds != self.config.confirm_seconds
        activity_threshold_changed = (
            config.activity_threshold_dbfs != self.config.activity_threshold_dbfs
        )
        self.config = config
        if threshold_changed or confirm_changed or activity_threshold_changed:
            self._above_seconds = 0.0
            self._below_seconds = 0.0

    def observe(self, level_dbfs: float, duration_seconds: float) -> bool:
        if not self.config.enabled:
            return False

        level = float(level_dbfs)
        duration = max(0.0, float(duration_seconds))
        threshold = self.config.threshold_dbfs

        if not self.armed:
            rearm_threshold = threshold - _REARM_MARGIN_DB
            if level <= rearm_threshold:
                self._below_seconds += duration
                self._above_seconds = 0.0
                if self._below_seconds >= _REARM_CONFIRM_SECONDS:
                    self.armed = True
                    self._above_seconds = 0.0
                return False

            self._below_seconds = 0.0
            # A low wake threshold can intentionally detect turntable power
            # rather than record activity. If auto-sleep happens while the
            # turntable remains powered on, waiting for a below-threshold edge
            # would otherwise block the next record forever. A sustained level
            # above the calibrated pickup/start threshold is therefore accepted
            # as a fresh activity transition as well.
            secondary_activity_wake = (
                self.config.activity_threshold_dbfs >= threshold + _REARM_MARGIN_DB
            )
            if secondary_activity_wake and level >= self.config.activity_threshold_dbfs:
                self._above_seconds += duration
                if self._above_seconds >= self.config.confirm_seconds:
                    self._above_seconds = 0.0
                    return True
            else:
                self._above_seconds = 0.0
            return False

        if level >= threshold:
            self._above_seconds += duration
            if self._above_seconds >= self.config.confirm_seconds:
                self._above_seconds = 0.0
                return True
        else:
            self._above_seconds = 0.0

        return False


def pcm16_dbfs(raw_bytes: bytes) -> float:
    """Return the dBFS RMS level of signed 16-bit PCM without creating a WAV."""
    if not raw_bytes:
        return _DBFS_FLOOR
    try:
        rms = float(audioop.rms(raw_bytes, 2))
    except (audioop.error, ValueError):
        return _DBFS_FLOOR
    if rms <= 0.0:
        return _DBFS_FLOOR
    return max(_DBFS_FLOOR, 20.0 * math.log10(rms / 32767.0))


def _monitor_audio_settings(cfg: dict) -> tuple[str, int, int]:
    audio_cfg = cfg.get("audio") or {}
    configured_input = str(audio_cfg.get("device_name_contains") or "").strip()
    try:
        sample_rate = max(8000, int(audio_cfg.get("sample_rate", 44100)))
    except (TypeError, ValueError):
        sample_rate = 44100
    try:
        channels = max(1, min(2, int(audio_cfg.get("channels", 1))))
    except (TypeError, ValueError):
        channels = 1
    return configured_input, sample_rate, channels


def wait_for_audio_wake(
    *,
    require_rearm: bool = False,
    level_callback: Callable[[float, float, dict], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> AudioWakeEvent | None:
    """Wait in a low-power PCM monitor until the configured Vinyl wake level.

    This deliberately avoids WAV creation and all Shazam/network work. The
    function blocks in whichever coordinator owns the low-power monitor.
    Returning ``None`` means auto wake was disabled or the caller asked the
    monitor to stop while it was active.
    """
    # Keep the hardware dependency lazy so config/state-machine tests can run
    # on development machines without PortAudio/sounddevice installed.
    import sounddevice as sd
    from vinylpi.core.audio_capture import auto_detect_usb_device

    initial_cfg = read_config()
    wake_cfg = VinylAutoWakeConfig.from_config(initial_cfg)
    if not wake_cfg.enabled:
        return None

    tracker = WakeThresholdTracker(wake_cfg, require_rearm=require_rearm)
    last_debug_at = 0.0
    last_armed = tracker.armed

    while True:
        if should_continue is not None and not should_continue():
            return None

        raw_cfg = read_config()
        wake_cfg = VinylAutoWakeConfig.from_config(raw_cfg)
        if not wake_cfg.enabled:
            return None
        tracker.configure(wake_cfg)

        configured_input, sample_rate, channels = _monitor_audio_settings(raw_cfg)
        block_frames = max(256, int(round(sample_rate * _MONITOR_BLOCK_SECONDS)))
        block_duration = block_frames / float(sample_rate)
        debug_log = bool((raw_cfg.get("debug") or {}).get("logs", False))

        device = auto_detect_usb_device()
        if device is None:
            if debug_log:
                print("Auto-wake monitor could not find the configured audio input; retrying.")
            time.sleep(_RETRY_SECONDS)
            continue

        try:
            with sd.RawInputStream(
                device=device,
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                blocksize=block_frames,
            ) as stream:
                if debug_log:
                    state = "armed" if tracker.armed else "waiting to re-arm"
                    print(
                        "Low-power Vinyl audio monitor active "
                        f"(wake >= {wake_cfg.threshold_dbfs:.1f} dBFS, {state})."
                    )

                while True:
                    if should_continue is not None and not should_continue():
                        return None

                    raw_cfg = read_config()
                    wake_cfg = VinylAutoWakeConfig.from_config(raw_cfg)
                    if not wake_cfg.enabled:
                        if bool((raw_cfg.get("debug") or {}).get("logs", False)):
                            print("Vinyl auto wake disabled; stopping low-power audio monitor.")
                        return None
                    tracker.configure(wake_cfg)
                    debug_log = bool((raw_cfg.get("debug") or {}).get("logs", False))

                    # Reopen the PortAudio stream when the selected input or its
                    # capture settings change in the web UI.
                    if _monitor_audio_settings(raw_cfg) != (configured_input, sample_rate, channels):
                        if debug_log:
                            print("Audio input settings changed; reopening auto-wake monitor.")
                        break

                    raw_audio, overflowed = stream.read(block_frames)
                    level_dbfs = pcm16_dbfs(bytes(raw_audio))

                    if level_callback is not None:
                        try:
                            level_callback(level_dbfs, block_duration, raw_cfg)
                        except Exception as exc:
                            if debug_log:
                                print(f"Auto-wake level callback failed: {exc}")

                    woke = tracker.observe(level_dbfs, block_duration)
                    now = time.monotonic()
                    if debug_log and (now - last_debug_at >= _DEBUG_INTERVAL_SECONDS):
                        state = "armed" if tracker.armed else "waiting to re-arm"
                        overflow_note = ", overflow" if overflowed else ""
                        print(
                            f"Auto-wake input: {level_dbfs:.1f} dBFS "
                            f"(threshold {wake_cfg.threshold_dbfs:.1f}, {state}{overflow_note})"
                        )
                        last_debug_at = now

                    if debug_log and tracker.armed != last_armed:
                        print("Vinyl auto-wake monitor re-armed after the input became quiet.")
                    last_armed = tracker.armed

                    if woke:
                        if debug_log:
                            print(
                                f"Vinyl auto wake triggered at {level_dbfs:.1f} dBFS; "
                                "starting Shazam recognition."
                            )
                        return AudioWakeEvent(
                            level_dbfs=level_dbfs,
                            threshold_dbfs=wake_cfg.threshold_dbfs,
                        )
        except Exception as exc:
            if debug_log:
                print(f"Low-power audio monitor failed: {exc}; retrying in {_RETRY_SECONDS:g}s.")
            time.sleep(_RETRY_SECONDS)
