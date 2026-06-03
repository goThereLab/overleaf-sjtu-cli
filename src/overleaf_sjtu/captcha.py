from __future__ import annotations

import os
import shutil
from io import BytesIO

from PIL import Image


def captcha_to_ansi_blocks(
    captcha_bytes: bytes,
    *,
    windows: bool | None = None,
) -> tuple[tuple[int, int], tuple[int, int], list[str]]:
    """Render a captcha for terminal display.

    POSIX terminals keep the truecolor half-block renderer. Windows uses an
    ASCII fallback because older ConHost/PowerShell sessions often strip or
    mangle 24-bit ANSI colors and Unicode half-block glyphs.
    """
    with Image.open(BytesIO(captcha_bytes)) as img:
        img = img.convert("RGB")
        original_size = img.size
        if _use_windows_fallback(windows):
            return original_size, *_captcha_to_ascii_rows(img)

        return original_size, *_captcha_to_truecolor_rows(img)


def _use_windows_fallback(windows: bool | None) -> bool:
    return os.name == "nt" if windows is None else windows


def _captcha_to_truecolor_rows(img: Image.Image) -> tuple[tuple[int, int], list[str]]:
    terminal_width = shutil.get_terminal_size((120, 24)).columns
    max_width = max(20, terminal_width - 2)

    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * scale))), Image.Resampling.LANCZOS)

    rendered_size = img.size
    pixels = img.load()
    rows = []

    for y in range(0, img.height, 2):
        parts = []
        for x in range(img.width):
            top = pixels[x, y]
            bottom = pixels[x, y + 1] if y + 1 < img.height else (255, 255, 255)
            parts.append(
                f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m"
                f"\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m"
                "▀"
            )
        rows.append("".join(parts) + "\x1b[0m")

    return rendered_size, rows


def _captcha_to_ascii_rows(img: Image.Image) -> tuple[tuple[int, int], list[str]]:
    terminal_width = shutil.get_terminal_size((120, 24)).columns
    max_width = max(20, terminal_width - 2)
    img = _crop_to_ink(img)

    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * scale))), Image.Resampling.LANCZOS)

    rendered_size = img.size
    pixels = img.load()
    rows = []
    for y in range(img.height):
        row = "".join("#" if _is_ink(pixels[x, y]) else " " for x in range(img.width)).rstrip()
        rows.append(row)
    return rendered_size, rows


def _crop_to_ink(img: Image.Image) -> Image.Image:
    pixels = img.load()
    xs = []
    ys = []
    for y in range(img.height):
        for x in range(img.width):
            if _is_ink(pixels[x, y]):
                xs.append(x)
                ys.append(y)
    if not xs or not ys:
        return img
    left = max(0, min(xs) - 1)
    upper = max(0, min(ys) - 1)
    right = min(img.width, max(xs) + 2)
    lower = min(img.height, max(ys) + 2)
    return img.crop((left, upper, right, lower))


def _is_ink(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    distance_from_white = (255 - r) + (255 - g) + (255 - b)
    return distance_from_white > 50 and (min(r, g, b) < 220 or max(r, g, b) - min(r, g, b) > 10)
