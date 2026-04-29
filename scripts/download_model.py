"""Pre-download the appropriate Whisper model for the detected hardware.

Called by setup.bat during first-time setup.  Detects whether the host
has a compatible NVIDIA GPU; if yes, downloads ``large-v3`` for CUDA
(float16); if no, downloads ``distil-large-v3`` for CPU (int8).

Runs detection without importing the ``tachyon`` package so it works in
the setup.bat environment before PYTHONPATH is configured.
"""

from __future__ import annotations

import sys


def _has_cuda() -> tuple[bool, str, float]:
    """Return ``(has_cuda, device_name, vram_gb)`` using torch.

    torch is always installed by setup.bat before this script runs (it
    is a hard dependency of speechbrain/resemblyzer), so we rely on it
    instead of the lighter NVML path.
    """
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            idx = 0
            name = torch.cuda.get_device_name(idx)
            props = torch.cuda.get_device_properties(idx)
            vram_gb = props.total_memory / (1024 ** 3)
            return True, name, vram_gb
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] torch CUDA check failed: {exc}", file=sys.stderr)
    return False, "", 0.0


def _recommend_model(has_cuda: bool, vram_gb: float) -> tuple[str, str, str]:
    """Return ``(model_size, device, compute_type)`` for the detected hw."""
    if has_cuda:
        if vram_gb >= 10.0:
            return "large-v3", "cuda", "float16"
        if vram_gb >= 6.0:
            return "medium", "cuda", "float16"
        return "small", "cuda", "float16"
    return "distil-large-v3", "cpu", "int8"


def main() -> int:
    has_cuda, name, vram_gb = _has_cuda()
    if has_cuda:
        print(f"  Detected GPU: {name} ({vram_gb:.1f} GB VRAM)")
    else:
        print("  No NVIDIA GPU detected — using CPU model.")

    model_size, device, compute_type = _recommend_model(has_cuda, vram_gb)
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
