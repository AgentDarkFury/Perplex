"""
Tray icons drawn at runtime (no image files to ship).

    python -m perplex.icon build/perplex.ico   writes the .exe icon
"""

from __future__ import annotations

import sys
from functools import lru_cache

from PIL import Image, ImageDraw

COLORS = {
    "playing": (229, 160, 13),  # amber
    "paused": (229, 160, 13),
    "idle": (96, 104, 118),  # slate
    "hidden": (96, 104, 118),
    "starting": (59, 130, 246),  # blue
    "signin": (59, 130, 246),
    "waiting": (59, 130, 246),
    "error": (217, 83, 79),  # red
}

_SCALE = 4  # draw large, then downsample for smooth edges


@lru_cache(maxsize=None)
def make_icon(kind: str = "playing", size: int = 64) -> Image.Image:
    s = size * _SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=COLORS.get(kind, COLORS["idle"]))
    white = (255, 255, 255, 255)

    if kind == "paused":
        w, h, gap = s * 0.14, s * 0.46, s * 0.1
        top = (s - h) / 2
        draw.rectangle((s / 2 - gap / 2 - w, top, s / 2 - gap / 2, top + h), fill=white)
        draw.rectangle((s / 2 + gap / 2, top, s / 2 + gap / 2 + w, top + h), fill=white)
    elif kind == "error":
        draw.rectangle((s * 0.44, s * 0.22, s * 0.56, s * 0.62), fill=white)
        draw.ellipse((s * 0.43, s * 0.68, s * 0.57, s * 0.82), fill=white)
    elif kind == "hidden":
        # play glyph, struck through
        draw.polygon([(s * 0.36, s * 0.27), (s * 0.36, s * 0.73), (s * 0.74, s * 0.5)], fill=(255, 255, 255, 140))
        draw.line((s * 0.2, s * 0.8, s * 0.8, s * 0.2), fill=white, width=int(s * 0.08))
    else:
        draw.polygon([(s * 0.36, s * 0.27), (s * 0.36, s * 0.73), (s * 0.74, s * 0.5)], fill=white)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: str) -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    make_icon("playing", 256).save(path, format="ICO", sizes=[(n, n) for n in sizes])


if __name__ == "__main__":
    write_ico(sys.argv[1] if len(sys.argv) > 1 else "perplex.ico")
