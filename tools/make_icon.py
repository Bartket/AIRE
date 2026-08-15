"""Generate the AIRE application icon and a PNG preview.

Run this only when the artwork changes:

    python tools/make_icon.py

The .ico is committed, so a normal build does not need Pillow to draw it.

**The artwork lives in `ai_race_engineer/static/ui/logo.svg`, not here.** The settings panel
loads that file directly, and this script parses the same paths — including
the transforms on them — so the icon and the panel cannot show different
marks. Edit the SVG, re-run this.

The mark is an angular "A" with the detached foot picked out in orange. The
foot is a separate path in the outline, so the two-tone split costs nothing:
it colours a shape that was already there.

**The fit changes with size.** The letterform is stylised enough that at
16px it reads as a mark rather than a letter, and letterboxing it inside a
generous margin wastes the few pixels there are. So the margin tightens as
the canvas shrinks — the same drawing, better used.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "ai_race_engineer" / "static" / "ui" / "logo.svg"
OUT = ROOT / "ai_race_engineer" / "static" / "icon.ico"
PREVIEW = ROOT / "ai_race_engineer" / "static" / "icon-preview.png"
SIZES = [16, 24, 32, 48, 64, 128, 256]

INK = (18, 20, 26, 255)        # tile
INK_TOP = (32, 36, 46, 255)    # subtle vertical lift
FALLBACK = (245, 247, 250, 255)

# Breathing room around the glyph, as a fraction of the tile. Tighter when
# small so the mark uses the pixels it has.
MARGIN_SMALL, MARGIN_LARGE = 0.06, 0.11
MARGIN_SETTLES_AT = 64

_NUM = r"-?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ── SVG transforms ──────────────────────────────────────────────────
# Only the forms this file uses. Parsed rather than hard-coded so that
# editing the SVG cannot silently leave the icon drawing something else.

def _matrix(transform):
    """A transform attribute -> (a, b, c, d, e, f)."""
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(rf"(matrix|translate|scale)\s*\(([^)]*)\)", transform or ""):
        v = [float(n) for n in re.findall(_NUM, args)]
        if name == "matrix":
            n = tuple(v[:6])
        elif name == "translate":
            n = (1.0, 0.0, 0.0, 1.0, v[0], v[1] if len(v) > 1 else 0.0)
        else:
            sx = v[0]
            sy = v[1] if len(v) > 1 else sx
            n = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        m = _compose(m, n)
    return m


def _compose(m, n):
    """Apply n inside m, the way nested SVG groups nest."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B,
            a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


# ── Path flattening ─────────────────────────────────────────────────

def _tokens(d):
    for m in re.finditer(rf"([MmLlHhVvCcZz])|({_NUM})", d):
        yield m.group(1) if m.group(1) else float(m.group(2))


def _flatten(d, steps=16):
    """One path -> a list of points.

    Curves are subdivided rather than approximated: the icon is drawn at
    8x and downsampled, so any faceting disappears in the resample.
    """
    pts, cur, start, cmd = [], (0.0, 0.0), (0.0, 0.0), None
    toks = list(_tokens(d))
    i = 0
    while i < len(toks):
        t = toks[i]
        if isinstance(t, str):
            cmd = t
            i += 1
            if cmd in "Zz":
                cur = start
            continue
        if cmd in ("M", "m"):
            x, y = toks[i], toks[i + 1]; i += 2
            cur = (x, y) if cmd == "M" else (cur[0] + x, cur[1] + y)
            start = cur
            pts.append(cur)
            cmd = "L" if cmd == "M" else "l"      # implicit lineto follows
        elif cmd in ("L", "l"):
            x, y = toks[i], toks[i + 1]; i += 2
            cur = (x, y) if cmd == "L" else (cur[0] + x, cur[1] + y)
            pts.append(cur)
        elif cmd in ("H", "h"):
            x = toks[i]; i += 1
            cur = (x, cur[1]) if cmd == "H" else (cur[0] + x, cur[1])
            pts.append(cur)
        elif cmd in ("V", "v"):
            y = toks[i]; i += 1
            cur = (cur[0], y) if cmd == "V" else (cur[0], cur[1] + y)
            pts.append(cur)
        elif cmd in ("C", "c"):
            v = toks[i:i + 6]; i += 6
            if cmd == "c":
                p1 = (cur[0] + v[0], cur[1] + v[1])
                p2 = (cur[0] + v[2], cur[1] + v[3])
                p3 = (cur[0] + v[4], cur[1] + v[5])
            else:
                p1, p2, p3 = (v[0], v[1]), (v[2], v[3]), (v[4], v[5])
            p0 = cur
            for s in range(1, steps + 1):
                u = s / steps
                w = 1 - u
                pts.append((
                    w ** 3 * p0[0] + 3 * w * w * u * p1[0]
                    + 3 * w * u * u * p2[0] + u ** 3 * p3[0],
                    w ** 3 * p0[1] + 3 * w * w * u * p1[1]
                    + 3 * w * u * u * p2[1] + u ** 3 * p3[1]))
            cur = p3
        else:
            i += 1
    return pts


def _fill(el):
    """The fill, whether it is an attribute or a style declaration.

    Editors differ: hand-written SVG tends to use fill="#RRGGBB", while
    anything exported writes style="fill:#RRGGBB". Reading only the first
    made every path fall back to white, which drew the mark as one blank
    tile — and it looked plausible enough at 16px to ship.
    """
    fill = (el.get("fill") or "").strip()
    if fill:
        return fill
    m = re.search(r"fill\s*:\s*([^;]+)", el.get("style") or "")
    return m.group(1).strip() if m else ""


def _colour(el):
    """A path's fill as RGBA.

    Both #RRGGBB and rgb(r,g,b) turn up — which one depends on the editor
    that last wrote the file, not on anything meaningful. An unrecognised
    form falls back to white, and white is also the colour of the largest
    shape here, so the failure looks like a design decision rather than a
    parse miss: the orange foot just quietly stops being orange.
    """
    fill = _fill(el)
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", fill)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    m = re.fullmatch(rf"rgb\(\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*\)", fill)
    if m:
        return tuple(round(float(m.group(i))) for i in (1, 2, 3)) + (255,)
    return FALLBACK


def load_glyph():
    """Every <path> in the SVG, with its transforms applied and its fill.

    The tile is skipped: this script draws it, because the gradient wants to
    be dithered per pixel rather than resampled. It is identified by being
    painted with a gradient reference rather than a flat colour, which holds
    whether it is written as a <rect> or as a rounded <path>.
    """
    root = ET.fromstring(SOURCE.read_text(encoding="utf-8"))
    out = []

    def walk(node, m):
        m = _compose(m, _matrix(node.get("transform")))
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "g":
                walk(child, m)
            elif tag == "path" and child.get("d"):
                if _fill(child).startswith("url("):
                    continue                       # the tile, drawn below
                cm = _compose(m, _matrix(child.get("transform")))
                out.append(([_apply(cm, x, y) for x, y in _flatten(child.get("d"))],
                            _colour(child)))

    walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    if not out:
        raise SystemExit(f"no <path> found in {SOURCE}")
    return out


GLYPH = load_glyph()
_XS = [p[0] for poly, _ in GLYPH for p in poly]
_YS = [p[1] for poly, _ in GLYPH for p in poly]
BOX = (min(_XS), min(_YS), max(_XS), max(_YS))


def draw_icon(size: int) -> Image.Image:
    """Draw at 8x then downsample — cheap, reliable antialiasing."""
    scale = 8
    px = size * scale
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))

    # Rounded tile with a top-to-bottom gradient.
    tile = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tile)
    for y in range(px):
        tdraw.line([(0, y), (px, y)], fill=_lerp(INK_TOP, INK, y / px))
    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, px - 1, px - 1], radius=px * 0.22, fill=255)
    img.paste(tile, (0, 0), mask)

    t = min(max((size - 16) / (MARGIN_SETTLES_AT - 16), 0.0), 1.0)
    margin = MARGIN_SMALL + (MARGIN_LARGE - MARGIN_SMALL) * t

    x0, y0, x1, y1 = BOX
    avail = px * (1 - 2 * margin)
    fit = min(avail / (x1 - x0), avail / (y1 - y0))
    ox = (px - (x1 - x0) * fit) / 2 - x0 * fit
    oy = (px - (y1 - y0) * fit) / 2 - y0 * fit

    draw = ImageDraw.Draw(img)
    for poly, colour in GLYPH:
        draw.polygon([(p[0] * fit + ox, p[1] * fit + oy) for p in poly], fill=colour)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(s) for s in SIZES]
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])

    # Contact sheet so the small sizes can be eyeballed.
    pad = 12
    sheet_w = sum(s + pad for s in SIZES) + pad
    sheet = Image.new("RGBA", (sheet_w, 256 + pad * 2), (60, 63, 70, 255))
    x = pad
    for frame, s in zip(frames, SIZES):
        sheet.paste(frame, (x, 256 + pad - s), frame)
        x += s + pad
    sheet.save(PREVIEW)

    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes, sizes: {SIZES})")
    print(f"Wrote {PREVIEW}")


if __name__ == "__main__":
    main()
