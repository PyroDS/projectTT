from __future__ import annotations

import json
from pathlib import Path

from tachyon.config import Config


def test_load_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "missing.json")
    # Fresh installs get hardware-aware auto-selection rather than a
    # hard-coded "large-v3" — see hardware.resolve_transcriber_config.
    assert cfg.model_size == "auto"
    assert cfg.compute_device == "auto"
    assert cfg.hotkey == "ctrl+shift+t"
    assert cfg.first_run_complete is False
    assert cfg.consent_acknowledged is False


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "model_size": "small",
                "unknown_key": "ignored",
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.model_size == "small"
    assert not hasattr(cfg, "unknown_key")


def test_load_bad_json_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{bad json", encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.model_size == "auto"


def test_save_and_reload_overlay_position_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config(overlay_position=(100, 200), output_dir=str(tmp_path / "out"))
    cfg.save(path)

    loaded = Config.load(path)
    assert loaded.overlay_position == (100, 200)
    assert loaded.output_dir == str(tmp_path / "out")


def test_get_active_loopback_devices_ignores_legacy_output_device() -> None:
    """Empty loopback_devices must mean 'use WASAPI default' — it must NOT
    auto-migrate the legacy ``output_device`` field. The auto-migration
    re-fired on every load and resurrected stale device names whenever a
    user cleared their loopback selection, which could then fail to resolve
    after a Windows device rename (e.g. "Arctis 7 Chat" → "2- Arctis 7 Chat").
    The default-loopback fallback lives in ``capture._resolve_loopback_targets``.
    """
    cfg = Config(output_device="Headphones (Device)")
    assert cfg.get_active_loopback_devices() == []


def test_get_active_loopback_devices_returns_configured_entries() -> None:
    cfg = Config(
        loopback_devices=[
            {"device_name": "A", "label": "Alpha", "enabled": True},
            {"device_name": "B", "label": "Beta", "enabled": False},
            {"device_name": "C", "label": "Gamma", "enabled": True},
        ]
    )
    devices = cfg.get_active_loopback_devices()
    assert [d.device_name for d in devices] == ["A", "C"]
    assert [d.label for d in devices] == ["Alpha", "Gamma"]

