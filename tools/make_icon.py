"""Genera ``client/theme/assets/app.ico`` dal logo (``logo.png``) nel formato piu' compatibile con Windows.

Le icone ``.exe`` di Windows (risorse RT_ICON) vanno meglio con voci BMP a 32 bit (BGRA + maschera AND) per le
dimensioni piccole e con PNG solo per i 256 px; il salvataggio standard di Pillow mette PNG ovunque.

Uso:   python tools/make_icon.py            (richiede Pillow:  pip install pillow)
"""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "client" / "theme" / "assets" / "logo.png"
DST = ROOT / "client" / "theme" / "assets" / "app.ico"
BMP_SIZES = (16, 20, 24, 32, 40, 48, 64, 72, 96, 128)
PNG_SIZES = (256,)


def _bmp_entry(img: Image.Image) -> bytes:
    w, h = img.size
    px = img.convert("RGBA")
    rows = []
    for y in range(h - 1, -1, -1):                                   # le righe BMP vanno dal basso verso l'alto
        rows.append(b"".join(struct.pack("<BBBB", b, g, r, a)
                             for (r, g, b, a) in (px.getpixel((x, y)) for x in range(w))))
    xor = b"".join(rows)
    mask_row = ((w + 31) // 32) * 4                                   # maschera AND a 1 bit, righe allineate a 32 bit
    and_mask = bytes(mask_row * h)                                    # tutto 0: la trasparenza e' nel canale alfa
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + xor + and_mask


def _png_entry(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def build(src: Path = SRC, dst: Path = DST) -> list[tuple[int, str, int]]:
    base = Image.open(src).convert("RGBA")
    entries: list[tuple[int, bytes, str]] = []
    for s in BMP_SIZES:
        entries.append((s, _bmp_entry(base.resize((s, s), Image.LANCZOS)), "bmp"))
    for s in PNG_SIZES:
        entries.append((s, _png_entry(base.resize((s, s), Image.LANCZOS)), "png"))
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for s, data, _k in entries:
        out += struct.pack("<BBBBHHII", 0 if s >= 256 else s, 0 if s >= 256 else s, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _s, data, _k in entries:
        out += data
    dst.write_bytes(bytes(out))
    return [(s, k, len(d)) for s, d, k in entries]


if __name__ == "__main__":
    for s, k, n in build():
        print(f"{s:>3}x{s:<3} {k}  {n:>6} byte")
    print(f"scritto {DST} ({DST.stat().st_size} byte)")
    sys.exit(0)
