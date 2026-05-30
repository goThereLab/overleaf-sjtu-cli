from __future__ import annotations

import shutil
from io import BytesIO

from PIL import Image


def captcha_to_ansi_blocks(captcha_bytes: bytes) -> tuple[tuple[int, int], tuple[int, int], list[str]]:
    with Image.open(BytesIO(captcha_bytes)) as img:
        img = img.convert("RGB")
        original_size = img.size
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

        return original_size, rendered_size, rows
