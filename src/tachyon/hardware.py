"""Hardware detection for GPU/CPU/VRAM awareness.

Determines the best compute device and model size for the host machine.
Used at startup to auto-select Whisper configuration and by the first-run
wizard to show the user what will be used.

The module intentionally avoids a hard torch dependency at import time so
it can be called before any heavy libraries load.  Detection is attempted
in priority order: NVML (fastest, no torch needed), then torch.cuda as a
fallback, then "CPU only".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HardwareInfo:
    """Detected hardware capabilities.

    Attributes:
        has_cuda: ``True`` if a CUDA-capable NVIDIA GPU was found AND the
            CUDA runtime appears usable.  Note: this does not guarantee
            faster-whisper will successfully load on CUDA — drivers or
            DLLs can still be missing.  The transcriber handles that case
            with its own fallback.
        cuda_device_name: Marketing name of the first GPU, e.g.
            ``"NVIDIA GeForce RTX 2080 Ti"``.  Empty string if no GPU.
        vram_gb: Total VRAM of the first GPU in gigabytes.  ``0.0`` if no
            GPU or if query failed.
        reason: Short human-readable string describing how the info was
            obtained.  Used for logging + diagnostics.
    """

    has_cuda: bool
    cuda_device_name: str
    vram_gb: float
    reason: str

    @property
    def summary(self) -> str:
        """One-line summary for logs and UI display."""
        if self.has_cuda:
            return f"{self.cuda_device_name} ({self.vram_gb:.1f} GB VRAM)"
        return "CPU (no compatible NVIDIA GPU detected)"


def detect_hardware() -> HardwareInfo:
    """Detect CUDA availability and VRAM using the fastest available path.

    The detection order is:
        1. ``pynvml`` / ``nvidia-ml-py`` — fast, no heavy imports.
        2. ``torch.cuda`` — works if torch is already installed.
        3. Fallback — assume CPU-only.

    Any detection failure is logged at DEBUG level and the next path is
    tried.  This function never raises.
    """
    # ------ Path 1: NVML ------
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        try:
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                name_raw = pynvml.nvmlDeviceGetName(handle)
                name = name_raw.decode() if isinstance(name_raw, bytes) else str(name_raw)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_gb = mem.total / (1024 ** 3)
                return HardwareInfo(
                    has_cuda=True,
                    cuda_device_name=name,
                    vram_gb=vram_gb,
                    reason=f"NVML detected {name} with {vram_gb:.1f} GB VRAM",
                )
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("NVML detection unavailable: %s", exc)

    # ------ Path 2: torch.cuda ------
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            idx = 0
            name = torch.cuda.get_device_name(idx)
            props = torch.cuda.get_device_properties(idx)
            vram_gb = props.total_memory / (1024 ** 3)
            return HardwareInfo(
                has_cuda=True,
                cuda_device_name=name,
                vram_gb=vram_gb,
                reason=f"torch detected {name} with {vram_gb:.1f} GB VRAM",
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("torch CUDA detection unavailable: %s", exc)

    # ------ Path 3: no GPU ------
    return HardwareInfo(
        has_cuda=False,
        cuda_device_name="",
        vram_gb=0.0,
        reason="No NVIDIA GPU detected via NVML or torch — running on CPU",
    )


# ---------------------------------------------------------------------------
# Model selection policy
# ---------------------------------------------------------------------------

def recommend_model_size(hw: HardwareInfo) -> str:
    """Return a sensible default Whisper model size for this hardware.

    Policy (tuned for faster-whisper + CTranslate2 float16 on CUDA,
    int8 on CPU):

        - GPU >= 10 GB VRAM:  ``large-v3``        (best quality)
        - GPU >=  6 GB VRAM:  ``medium``          (good quality, fits)
        - GPU <   6 GB VRAM:  ``small``           (minimal VRAM usage)
        - CPU only:           ``distil-large-v3`` (best CPU quality/speed)

    The ``distil-large-v3`` model is ~2x faster than ``large-v3`` on CPU
    with near-identical accuracy — it's the current best choice for
    NVIDIA-less users.
    """
    if hw.has_cuda:
        if hw.vram_gb >= 10.0:
            return "large-v3"
        if hw.vram_gb >= 6.0:
            return "medium"
        return "small"
    return "distil-large-v3"


def recommend_compute_type(device: str) -> str:
    """Return an appropriate CTranslate2 compute type for a device.

    ``float16`` is the standard GPU compute type for faster-whisper on
    modern NVIDIA cards.  ``int8`` is the fastest usable CPU quantization
    — roughly 2-4x faster than ``float32`` on CPU with minimal accuracy
    loss for speech recognition.
    """
    if device == "cuda":
        return "float16"
    return "int8"


def resolve_transcriber_config(
    requested_device: str,
    requested_model_size: str,
) -> tuple[str, str, str, HardwareInfo]:
    """Resolve ``"auto"`` values to concrete device / model / compute type.

    Parameters:
        requested_device: ``"auto"``, ``"cuda"``, or ``"cpu"``.
        requested_model_size: ``"auto"`` or a specific Whisper model name
            like ``"large-v3"``, ``"medium"``, ``"small"``,
            ``"distil-large-v3"``, etc.

    Returns:
        A 4-tuple of ``(device, model_size, compute_type, hardware_info)``
        where all three strings are concrete (never ``"auto"``) and
        ``hardware_info`` is the detected host capabilities.
    """
    hw = detect_hardware()
    logger.info("Hardware detection: %s", hw.reason)

    # --- Resolve device ---
    if requested_device == "auto":
        device = "cuda" if hw.has_cuda else "cpu"
    elif requested_device == "cuda":
        if not hw.has_cuda:
            logger.warning(
                "Config requested CUDA but no GPU detected — falling back to CPU.",
            )
            device = "cpu"
        else:
            device = "cuda"
    else:
        device = "cpu"

    # --- Resolve model size ---
    if requested_model_size == "auto":
        model_size = recommend_model_size(hw)
    else:
        model_size = requested_model_size

    # --- Compute type follows device ---
    compute_type = recommend_compute_type(device)

    logger.info(
        "Transcriber config resolved: device=%s, model=%s, compute_type=%s",
        device, model_size, compute_type,
    )
    return device, model_size, compute_type, hw
