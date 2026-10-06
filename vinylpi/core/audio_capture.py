from __future__ import annotations

import io
import re

import sounddevice as sd
import soundfile as sf

from vinylpi.config.runtime import read_config


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _default_input_index() -> int | None:
    try:
        index = int(sd.default.device[0])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    return index if index >= 0 else None


_ALSA_VIRTUAL_INPUT_NAMES = {
    "default",
    "sysdefault",
    "spdif",
    "iec958",
    "dmix",
    "dsnoop",
    "front",
    "pulse",
    "pipewire",
    "jack",
    "oss",
    "samplerate",
    "speexrate",
    "upmix",
    "vdownmix",
}


def _clean_audio_device_label(name: str) -> str:
    """Return a consumer-facing label while keeping the raw PortAudio name internal."""
    raw = str(name or "").strip()
    if not raw:
        return "Audio input"

    # ALSA's hardware entries commonly look like
    # ``USB AUDIO CODEC: Audio (hw:0,0)``. The hw address is useful internally,
    # but it is implementation detail in the Settings UI.
    cleaned = re.sub(
        r"\s*:\s*Audio\s*\(hw:\d+,\d+\)\s*$",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or raw


def _is_alsa_virtual_input(name: str, host_api_name: str) -> bool:
    """Identify common ALSA aliases/plugins that are not separate physical inputs."""
    if "alsa" not in str(host_api_name or "").casefold():
        return False

    normalized = re.sub(r"\s+", " ", str(name or "").strip().casefold())
    if not normalized:
        return False

    # Handle both plain aliases (``default``) and parameterized forms such as
    # ``sysdefault:CARD=...`` without hiding descriptive hardware names.
    alias = re.split(r"[:=,\s]", normalized, maxsplit=1)[0]
    if alias in _ALSA_VIRTUAL_INPUT_NAMES:
        return True
    return alias.startswith("surround")


def _query_audio_input_devices() -> list[dict]:
    devices = sd.query_devices()
    try:
        host_apis = sd.query_hostapis()
    except Exception:
        host_apis = []

    default_input = _default_input_index()
    result: list[dict] = []

    for index, device in enumerate(devices):
        max_inputs = _safe_int(device.get("max_input_channels"), 0)
        if max_inputs <= 0:
            continue

        name = str(device.get("name") or f"Input {index}").strip()
        host_api_name = ""
        host_api_index = _safe_int(device.get("hostapi"), -1)
        if 0 <= host_api_index < len(host_apis):
            try:
                host_api_name = str(host_apis[host_api_index].get("name") or "").strip()
            except (AttributeError, TypeError):
                host_api_name = ""

        result.append(
            {
                "index": index,
                "name": name,
                "display_name": _clean_audio_device_label(name),
                "max_input_channels": max_inputs,
                "hostapi": host_api_name,
                "is_default": index == default_input,
                "is_virtual": _is_alsa_virtual_input(name, host_api_name),
            }
        )

    return result


def list_audio_input_devices() -> list[dict]:
    """Return user-selectable capture devices, hiding redundant ALSA aliases.

    PortAudio exposes ALSA plugin aliases such as ``default`` and ``sysdefault``
    alongside the underlying hardware. They are useful plumbing but confusing
    as separate choices. Prefer physical/descriptive inputs in the UI. If a
    system exposes *only* virtual inputs (for example a Pulse/PipeWire bridge),
    keep them as a fallback so VinylPi remains usable on other Linux stacks.
    """
    devices = _query_audio_input_devices()
    physical = [device for device in devices if not device["is_virtual"]]
    return physical or devices


def auto_detect_usb_device() -> int | None:
    """Resolve the configured input device while keeping legacy name matching.

    ``audio.device_name_contains`` is retained for backwards compatibility with
    existing configs. The Settings UI now stores a full device name. An empty
    value means Automatic: prefer the system default capture device, otherwise
    use the first available input.
    """
    cfg = read_config()
    configured_name = str(cfg["audio"].get("device_name_contains") or "").strip()
    debug_log = bool(cfg["debug"].get("logs", False))

    try:
        all_devices = _query_audio_input_devices()
    except Exception as exc:
        print(f"Could not query audio devices: {exc}")
        return None

    if not all_devices:
        print("No audio input devices are available.")
        return None

    if not configured_name:
        visible_devices = [device for device in all_devices if not device["is_virtual"]] or all_devices
        selected = next((device for device in visible_devices if device["is_default"]), visible_devices[0])
        if debug_log:
            print(f"Using audio input: #{selected['index']} -> {selected['name']}")
        return int(selected["index"])

    # Resolve saved names against every PortAudio input so older configs that
    # intentionally selected an ALSA alias continue to work even though those
    # aliases are hidden from the normal Settings dropdown.
    devices = all_devices
    wanted = configured_name.casefold()

    # New configs store the selected full device name, so prefer an exact match.
    selected = next(
        (device for device in devices if str(device["name"]).casefold() == wanted),
        None,
    )

    # Older configs used a free-text substring such as "USB AUDIO".
    if selected is None:
        selected = next(
            (device for device in devices if wanted in str(device["name"]).casefold()),
            None,
        )

    if selected is not None:
        if debug_log:
            print(f"Using audio input: #{selected['index']} -> {selected['name']}")
        return int(selected["index"])

    print(
        f"Configured audio input '{configured_name}' was not found. "
        "Choose an available input in Settings > Audio."
    )
    return None


def record_sample(seconds_override: float | None = None) -> bytes | None:
    cfg = read_config()
    debug_log = bool(cfg["debug"].get("logs", False))
    audio_cfg = cfg["audio"]
    debug_cfg = cfg["debug"]

    sample_rate = int(audio_cfg["sample_rate"])
    seconds = max(0.5, float(seconds_override if seconds_override is not None else audio_cfg["sample_seconds"]))
    channels = max(1, int(audio_cfg["channels"]))
    debug_wav_path = str(debug_cfg.get("wav_path") or "")

    device = auto_detect_usb_device()
    if device is None:
        return None

    try:
        audio = sd.rec(
            int(seconds * sample_rate),
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            device=device,
        )
        sd.wait()
    except Exception as exc:
        print(f"Audio recording failed: {exc}")
        return None

    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="WAV")
    wav_bytes = buffer.getvalue()

    if debug_wav_path:
        try:
            sf.write(debug_wav_path, audio, sample_rate, format="WAV")
            if debug_log:
                print(f"Saved WAV file at: {debug_wav_path}")
        except Exception as exc:
            print(f"Could not save debug WAV: {exc}")

    return wav_bytes
