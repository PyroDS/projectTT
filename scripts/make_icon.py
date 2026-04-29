"""Generate ``assets/icon.ico`` for the Windows installer and shortcuts.

The in-app tray icon is drawn programmatically by ``ui/tray.py`` and we
keep that for consistency at runtime.  The Windows installer, the
Start Menu shortcut, and ``TachyonTranscripts.exe``'s embedded icon
however need a real ``.ico`` file on disk — this script writes one.

Uses the same visual design as the tray icon (dark circle with a white
"T", no recording-dot) and packs multiple resolutions into a single
multi-size ICO so Windows picks the right one at each DPI.

Run:
    python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The Windows ICO format allows multiple images in one file.  Shipping
# 16/32/48/64/128/256 covers Explorer, taskbar, Alt-Tab, Start Menu,
# and the installer's header icon at every DPI scale we care about.
_SIZES = (16, 32, 48, 64, 128, 256)


def _render(size: int) -> Image.Image:
    """Draw one frame of the icon at *size* x *size* pixels."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Dark circle with a thin anti-alias padding.
    padding = max(1, size // 32)
    draw.ellipse(
        [padding, padding, size - padding, size - padding],
        fill=(40, 40, 40, 240),
    )

    # Letter "T" centred.  Use Arial if available, default bitmap
    # font otherwise (mostly for environments without system fonts).
    font_size = int(size * 0.55)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "T", font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) / 2 - bbox[0]
    y = (size - text_h) / 2 - bbox[1]
    draw.text((x, y), "T", fill=(255, 255, 255, 255), font=font)
    return image


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    out_dir = project_root / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "icon.ico"

    frames = [_render(s) for s in _SIZES]

    # PIL.Image.save with format="ICO" accepts a sizes= tuple to
    # embed every frame in one file.
    frames[-1].save(
        out_path,
        format="ICO",
        sizes=[(s, s) for s in _SIZES],
        append_images=frames[:-1],
    )

    print(f"Wrote {out_path} ({', '.join(str(s) for s in _SIZES)} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
