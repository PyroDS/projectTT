"""User configuration with JSON persistence and sensible defaults."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Project root is two levels up from this file: src/tachyon/config.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"


@dataclass
class LoopbackDevice:
    """A configured loopback capture device.

    Attributes:
        device_name: WASAPI output device name (substring match), or None
            for the system default output device.
        label: Short label for this device (e.g. "Chat", "Game").
            Used in source tags and display labels.
        enabled: Whether to capture from this device.
    """

    device_name: Optional[str] = None
    label: str = ""
    enabled: bool = True


@dataclass
class Config:
    """Application configuration with defaults that work out of the box."""

    output_dir: str = ""  # Empty = PROJECT_ROOT/output
    model_size: str = "auto"  # "auto" picks by hardware; or "large-v3"/"medium"/"small"/"distil-large-v3"
    compute_device: str = "auto"  # "auto", "cuda", or "cpu"
    hotkey: str = "ctrl+shift+t"
    overlay_position: Optional[tuple[int, int]] = None  # None = auto center-bottom
    overlay_opacity: float = 0.8
    mic_device: Optional[str] = None  # None = system default
    output_device: Optional[str] = None  # None = system default (legacy, for migration)
    loopback_devices: list[dict] = field(default_factory=list)
    diarize_backend: str = "speechbrain"  # "speechbrain", "pyannote", or "resemblyzer"
    hf_token: str = ""  # HuggingFace token for pyannote backend
    reviewer_geometry: Optional[str] = None  # "WxH+X+Y" format, None = default
    overlay_expanded_size: Optional[tuple[int, int]] = None  # (W, H) for expanded overlay
    first_run_complete: bool = False  # True once user finishes the first-run wizard
    consent_acknowledged: bool = False  # True once user acknowledges recording-law disclaimer

    def get_active_loopback_devices(self) -> list[LoopbackDevice]:
        """Return the list of enabled LoopbackDevice objects.

        An empty list means "use the WASAPI system default output for
        loopback capture" — resolved downstream in ``capture.py``.

        The legacy ``output_device`` field is intentionally NOT migrated
        here: doing so resurrects stale device names after a Windows
        rename (e.g. ``"Arctis 7 Chat"`` → ``"2- Arctis 7 Chat"``) and
        defeats the system-default fallback in
        ``capture._resolve_loopback_targets``.  See
        ``tests/test_config.py::test_get_active_loopback_devices_ignores_legacy_output_device``.
        """
        devices: list[LoopbackDevice] = []
        for d in self.loopback_devices:
            if d.get("enabled", True):
                devices.append(LoopbackDevice(
                    device_name=d.get("device_name"),
                    label=d.get("label", ""),
                    enabled=True,
                ))
        return devices

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> Config:
        """Load config from a JSON file, merged with defaults.

        If the file does not exist, returns a Config with all defaults.
        Unknown keys in the JSON file are silently ignored.
        """
        path = Path(path)
        if not path.exists():
            logger.info("No config file at %s — using defaults", path)
            return cls()

        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read config from %s (%s) — using defaults", path, exc)
            return cls()

        # overlay_position comes in as a JSON array; convert to tuple
        if "overlay_position" in data and data["overlay_position"] is not None:
            data["overlay_position"] = tuple(data["overlay_position"])

        # overlay_expanded_size comes in as a JSON array; convert to tuple
        if "overlay_expanded_size" in data and data["overlay_expanded_size"] is not None:
            data["overlay_expanded_size"] = tuple(data["overlay_expanded_size"])

        # loopback_devices is stored as a list of dicts — pass through as-is
        # (the list[dict] field handles it natively)

        # Only pass keys that the dataclass actually accepts
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}

        return cls(**filtered)

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        """Write the current config to a JSON file.

        Creates parent directories if they don't exist.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = asdict(self)
        # Convert tuple to list for JSON serialization (tuples become arrays)
        if data["overlay_position"] is not None:
            data["overlay_position"] = list(data["overlay_position"])
        if data["overlay_expanded_size"] is not None:
            data["overlay_expanded_size"] = list(data["overlay_expanded_size"])

        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("Config saved to %s", path)

    def get_output_path(self) -> Path:
        """Return the output directory as an absolute Path."""
        if not self.output_dir:
            return PROJECT_ROOT / "output"
        return Path(self.output_dir).resolve()
