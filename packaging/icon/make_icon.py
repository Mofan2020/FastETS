"""Generate the FastETS application icon.

The icon is a macOS style rounded square with a luminous gradient and a
speech waveform - it has to stay readable at 32 px, so the mark is bold and
the background stays dark.

    python packaging/icon/make_icon.py

Produces:
    packaging/icon/icon.png        1024 x 1024 master
    packaging/icon/FastETS.ico     Windows icon (16 ... 256)
    packaging/icon/FastETS.icns    macOS icon (created with ``iconutil``)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
MASTER = 1024
SCALE = 4  # supersampling factor for smooth edges


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(size: int) -> Image.Image:
    """A diagonal indigo -> violet -> cyan gradient."""
    top = (0x35, 0x1C, 0x8C)
    middle = (0x6B, 0x3F, 0xF0)
    bottom = (0x1E, 0xB8, 0xD8)
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * (size - 1))
            if t < 0.55:
                color = _lerp(top, middle, t / 0.55)
            else:
                color = _lerp(middle, bottom, (t - 0.55) / 0.45)
            pixels[x, y] = color
    return image


def _rounded_mask(size: int, radius_ratio: float = 0.2237) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * radius_ratio), fill=255
    )
    return mask


def _highlight(size: int) -> Image.Image:
    """A soft light source in the upper left, like the macOS Big Sur icons."""
    layer = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(layer)
    radius = int(size * 0.62)
    draw.ellipse(
        (int(size * 0.18) - radius // 2, int(size * 0.10) - radius // 2,
         int(size * 0.18) + radius // 2, int(size * 0.10) + radius // 2),
        fill=70,
    )
    return layer.filter(ImageFilter.GaussianBlur(size * 0.09))


def _waveform(size: int) -> Image.Image:
    """The mark: five rounded bars that read as a sound wave."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    heights = [0.30, 0.52, 0.78, 0.52, 0.30]
    bar_width = int(size * 0.072)
    gap = int(size * 0.058)
    total = len(heights) * bar_width + (len(heights) - 1) * gap
    x = (size - total) // 2
    centre = size // 2
    for index, factor in enumerate(heights):
        height = int(size * factor)
        top = centre - height // 2
        draw.rounded_rectangle(
            (x, top, x + bar_width, top + height),
            radius=bar_width // 2,
            fill=(255, 255, 255, 238),
        )
        x += bar_width + gap
    return layer


def build_master() -> Image.Image:
    size = MASTER * SCALE
    base = _gradient(size).convert("RGBA")
    base.putalpha(_rounded_mask(size))

    glow = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    glow.putalpha(_highlight(size))
    base = Image.alpha_composite(base, glow)

    # A faint darker rim at the bottom gives the tile some depth.
    shade = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    shade_draw.ellipse(
        (int(size * 0.05), int(size * 0.72), int(size * 0.95), int(size * 1.30)),
        fill=(0, 0, 0, 60),
    )
    shade = shade.filter(ImageFilter.GaussianBlur(size * 0.06))
    shade.putalpha(Image.composite(shade.getchannel("A"), Image.new("L", (size, size), 0), _rounded_mask(size)))
    base = Image.alpha_composite(base, shade)

    mark = _waveform(size)
    base = Image.alpha_composite(base, mark)
    return base.resize((MASTER, MASTER), Image.LANCZOS)


def main() -> int:
    master = build_master()
    master.save(HERE / "icon.png")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(HERE / "FastETS.ico", format="ICO", sizes=ico_sizes)
    print(f"[icon] wrote {HERE / 'icon.png'}")
    print(f"[icon] wrote {HERE / 'FastETS.ico'}")

    # macOS needs an .iconset folder that iconutil turns into an .icns.
    iconset = HERE / "FastETS.iconset"
    iconset.mkdir(exist_ok=True)
    entries = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in entries.items():
        master.resize((size, size), Image.LANCZOS).save(iconset / name)
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "FastETS.icns")],
            check=True,
        )
        print(f"[icon] wrote {HERE / 'FastETS.icns'}")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[icon] iconutil failed (only available on macOS): {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
