from __future__ import annotations

import threading
import time

from vinylpi.config.runtime import read_config
from vinylpi.core.audio_wake import VinylAutoWakeConfig, wait_for_audio_wake
from vinylpi.core.pickup_usage import PickupUsageConfig, PickupUsageTracker
from vinylpi.core.stats_db import add_pickup_usage_seconds

_IDLE_POLL_SECONDS = 1.0
_PICKUP_FLUSH_SECONDS = 30.0

_lock = threading.RLock()
_stop_event = threading.Event()
_pause_event = threading.Event()
_monitoring_event = threading.Event()
_thread: threading.Thread | None = None


def is_running() -> bool:
    with _lock:
        return bool(_thread is not None and _thread.is_alive())


def is_monitoring_audio() -> bool:
    return _monitoring_event.is_set()


def start() -> bool:
    """Start the always-on Vinyl wake coordinator once for the web process."""
    global _thread, _stop_event
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event = threading.Event()
        _pause_event.clear()
        _thread = threading.Thread(
            target=_run,
            name="vinylpi-auto-wake",
            daemon=True,
        )
        _thread.start()
        return True


def stop(timeout: float = 2.0) -> bool:
    global _thread
    with _lock:
        thread = _thread
        if thread is None:
            return False
        _stop_event.set()
        _pause_event.set()

    if thread is not threading.current_thread():
        thread.join(timeout=max(0.0, float(timeout)))

    with _lock:
        if _thread is thread and not thread.is_alive():
            _thread = None
    return True


def pause_for_source_change(timeout: float = 1.5) -> None:
    """Release the USB input before a manually selected source starts.

    ``RawInputStream.read`` uses 0.5 s blocks, so a short bounded wait is enough
    for the monitor to notice the pause flag and close its PortAudio stream.
    """
    _pause_event.set()
    if threading.current_thread() is _thread:
        return

    deadline = time.monotonic() + max(0.0, float(timeout))
    while _monitoring_event.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)


def resume() -> None:
    _pause_event.clear()


def _should_monitor_source() -> bool:
    if _stop_event.is_set() or _pause_event.is_set():
        return False
    # Lazy import avoids a module cycle: source.py also pauses this service when
    # the user selects Vinyl/Spotify manually.
    from vinylpi.web.services.source import get_mode

    return get_mode() == "off"


def _debug(message: str) -> None:
    try:
        enabled = bool((read_config().get("debug") or {}).get("logs", False))
    except Exception:
        enabled = False
    if enabled:
        print(message)


def _wait_for_wake(*, require_rearm: bool):
    """Monitor raw PCM while the dashboard source is Off.

    Pickup usage is fed from the same low-power samples so the wake monitor does
    not need a second audio stream. Writes are batched to limit SD-card churn.
    """
    pickup_tracker = PickupUsageTracker(PickupUsageConfig.from_config(read_config()))
    pending_pickup_seconds = 0.0
    last_flush_at = time.monotonic()

    def on_level(level_dbfs: float, duration_seconds: float, raw_cfg: dict) -> None:
        nonlocal pending_pickup_seconds, last_flush_at
        pickup_tracker.configure(PickupUsageConfig.from_config(raw_cfg))
        if pickup_tracker.config.enabled:
            pending_pickup_seconds += pickup_tracker.observe(level_dbfs, duration_seconds)

        now = time.monotonic()
        if pending_pickup_seconds > 0 and now - last_flush_at >= _PICKUP_FLUSH_SECONDS:
            add_pickup_usage_seconds(pending_pickup_seconds)
            pending_pickup_seconds = 0.0
            last_flush_at = now

    _monitoring_event.set()
    try:
        event = wait_for_audio_wake(
            require_rearm=require_rearm,
            level_callback=on_level,
            should_continue=_should_monitor_source,
        )
    finally:
        _monitoring_event.clear()
        if pending_pickup_seconds > 0:
            add_pickup_usage_seconds(pending_pickup_seconds)

    return event


def _run() -> None:
    # Service startup while already Off is allowed to wake from the current
    # level. After any active source transitions back to Off, require a genuine
    # re-arm/transition so a persistent signal cannot create a sleep/wake loop.
    previous_mode = "off"
    require_rearm = False

    while not _stop_event.is_set():
        try:
            from vinylpi.web.services import source

            mode = source.get_mode()
            if mode != "off":
                previous_mode = mode
                require_rearm = True
                _stop_event.wait(_IDLE_POLL_SECONDS)
                continue

            if previous_mode != "off":
                require_rearm = True
            previous_mode = "off"

            if _pause_event.is_set():
                _stop_event.wait(0.1)
                continue

            wake_cfg = VinylAutoWakeConfig.from_config(read_config())
            if not wake_cfg.enabled:
                _stop_event.wait(_IDLE_POLL_SECONDS)
                continue

            event = _wait_for_wake(require_rearm=require_rearm)
            if _stop_event.is_set():
                break
            if event is None:
                _stop_event.wait(0.2)
                continue

            # wait_for_audio_wake has already left the RawInputStream here, so
            # recognition can safely claim the USB device.
            if source.get_mode() == "off" and not _pause_event.is_set():
                _debug(
                    f"Auto-wake switching dashboard to Vinyl at {event.level_dbfs:.1f} dBFS."
                )
                source.set_mode("vinyl", _from_auto_wake=True)
                previous_mode = "vinyl"
                require_rearm = True
        except Exception as exc:
            _monitoring_event.clear()
            _debug(f"Background Vinyl auto-wake monitor failed: {exc}; retrying.")
            _stop_event.wait(5.0)
