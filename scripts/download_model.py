"""Pre-download the appropriate Whisper model for the detected hardware.

Called by setup.bat during first-time setup. Uses the same hardware
resolution policy as runtime (`tachyon.hardware.resolve_transcriber_config`)
so setup pre-downloads the model the app will actually try to use.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_src_path() -> None:
    """Ensure ``src/`` is importable when running from project root."""
    project_root = Path(__file__).resolve().parent.parent
    src_path = project_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


def _resolve_target_model() -> tuple[str, str, str, str]:
    """Return ``(model_size, device, compute_type, hw_summary)``."""
    _bootstrap_src_path()
    try:
        from tachyon.hardware import resolve_transcriber_config
        device, model_size, compute_type, hw = resolve_transcriber_config(
            requested_device="auto",
            requested_model_size="auto",
        )
        return model_size, device, compute_type, hw.summary
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"hardware resolution failed: {exc}") from exc


def main() -> int:
    try:
        model_size, device, compute_type, hw_summary = _resolve_target_model()
        print(f"  Detected hardware: {hw_summary}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {exc}", file=sys.stderr)
        print("  Falling back to CPU default model selection.")
        model_size, device, compute_type = "distil-large-v3", "cpu", "int8"

    print(f"  Downloading '{model_size}' for {device} ({compute_type}) ...")

    from faster_whisper import WhisperModel  # type: ignore

    try:
        WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as exc:  # noqa: BLE001
        print(f"  [error] Model download failed: {exc}", file=sys.stderr)
        # Don't return 1 — setup.bat should continue.  The transcriber
        # will download on first run.
        return 0

    print(f"  '{model_size}' ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
