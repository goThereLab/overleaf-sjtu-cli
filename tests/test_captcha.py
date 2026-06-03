from __future__ import annotations

from io import BytesIO

from PIL import Image

from overleaf_sjtu.captcha import captcha_to_ansi_blocks


def _png_bytes() -> bytes:
    img = Image.new("RGB", (8, 5), "white")
    pixels = img.load()
    for y in range(1, 4):
        pixels[1, y] = (0, 180, 0)
        pixels[2, y] = (0, 180, 0)
    for x in range(4, 7):
        pixels[x, 1] = (20, 120, 20)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_captcha_truecolor_renderer_uses_half_blocks() -> None:
    original, rendered, rows = captcha_to_ansi_blocks(_png_bytes(), windows=False)

    assert original == (8, 5)
    assert rendered == (8, 5)
    assert rows
    assert "\x1b[38;2;" in rows[0]
    assert "▀" in rows[0]


def test_captcha_windows_renderer_uses_ascii_fallback() -> None:
    original, rendered, rows = captcha_to_ansi_blocks(_png_bytes(), windows=True)

    assert original == (8, 5)
    assert rendered[0] <= original[0]
    assert rows
    assert any("#" in row for row in rows)
    assert all("\x1b[" not in row for row in rows)
    assert all("▀" not in row for row in rows)
