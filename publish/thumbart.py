"""The drawing code behind custom thumbnails: pure Pillow, no ffmpeg, no network.

Two designs, both 1280x720 (or 1080x1920 for a Short), drawn at twice the size
and scaled down for smooth edges:

- frame: a real picture from the recording fills the thumbnail, a dark band
  that becomes solid over the bottom ~45%, a yellow bar, the label in very bold
  capitals (two lines at most, one key part in yellow), the team chip just above the yellow bar
  and "FloLabs" bottom-right;
- card (templates statement, diagram, hardware, concept): a dark gradient in
  one of a few fixed palettes, the team chip, a yellow bar, the title in very
  bold capitals (1-3 lines, one line or word in the accent colour), an optional
  smaller subtitle, "FloLabs" bottom-left and a simple illustration from a
  fixed library of vector-like drawings on the right.

Text never leaves its column: a long title is wrapped again and shrunk until it
fits, and the column ends before the illustration starts.
"""

from __future__ import annotations

import itertools
import math
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SS = 2  # supersampling: drawn at 2x, scaled down
YELLOW = "#ffcc1a"
NAVY = "#14213d"
NAVY2 = "#1f2f52"
TEAL = "#13a39b"
CORAL = "#ff8a7a"
WHITE = "#ffffff"
GREY = "#c9d2d1"
FRAME_SHADE = (11, 18, 32)

TEMPLATES = ("statement", "diagram", "hardware", "concept")
ART_IDS = ("network", "doc_to_code", "chip_mic", "robot_loop", "robot_arm", "rocket", "satellite", "brain",
           "heart", "gear", "database", "chart", "camera", "wifi", "cloud", "globe")
# id: (background kind, stops, the colour of the accent line)
PALETTES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "violet": ("radial", ("#3a1f78", "#160d33", "#0b0720"), YELLOW),
    "teal": ("linear", ("#0f3d3a", "#0b2524"), YELLOW),
    "rust": ("linear", ("#7a2e12", "#3d1508"), YELLOW),
    "navy": ("linear", ("#14213d", "#0a1226"), TEAL),
    "plum": ("linear", ("#5b1a4a", "#2a0a22"), YELLOW),
    "slate": ("linear", ("#24384a", "#101c27"), YELLOW),
    "forest": ("linear", ("#1e4d2b", "#0e2615"), YELLOW),
}
TEMPLATE_DEFAULTS = {  # template: (art, palette)
    "statement": ("doc_to_code", "teal"),
    "diagram": ("network", "violet"),
    "hardware": ("chip_mic", "rust"),
    "concept": ("robot_loop", "navy"),
}
ART_BOX = (560, 720)  # the space every illustration is drawn in

# ------------------------------------------------------------------- fonts

_ASSET_FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
HEAVY_FONT = "Montserrat-ExtraBold.ttf"  # titles, labels, the chip and the brand
REGULAR_FONT = "Montserrat-SemiBold.ttf"  # subtitles
_HEAVY_WORDS = ("black", "extrabold", "heavy", "bold")  # heaviest first, for any other OFL font dropped in


def _asset_fonts() -> tuple[Path | None, Path | None]:
    """(heavy, regular) from assets/fonts/: Montserrat ExtraBold and SemiBold by
    name, else the heaviest and a medium font found there; None for what is
    missing."""
    if not _ASSET_FONTS.is_dir():
        return None, None
    files = sorted(p for p in _ASSET_FONTS.iterdir() if p.suffix.lower() in (".ttf", ".otf"))
    by_name = {p.name.lower(): p for p in files}
    heavy = by_name.get(HEAVY_FONT.lower())
    regular = by_name.get(REGULAR_FONT.lower())
    if heavy is None:
        for word in _HEAVY_WORDS:
            heavy = next((p for p in files if word in p.stem.lower() and "semi" not in p.stem.lower()), None)
            if heavy:
                break
    if regular is None:
        regular = next((p for p in files if any(w in p.stem.lower() for w in ("semibold", "medium", "regular"))), None)
    return heavy, regular


@lru_cache(maxsize=1)
def font_files() -> tuple[Path | None, float, Path | None]:
    """(heavy font, how much to thicken it, regular font). Montserrat ExtraBold
    and SemiBold from assets/fonts/ (an OFL font, shipped in the Docker image)
    are used as they are. Only when those files are missing, the Bold that
    ships inside reportlab (already a dependency) is thickened with an outline,
    so a picture still comes out on every machine."""
    heavy, regular = _asset_fonts()
    if heavy:
        return heavy, 0.0, regular or heavy
    try:
        import reportlab

        folder = Path(reportlab.__file__).parent / "fonts"
        if (folder / "VeraBd.ttf").is_file():
            return folder / "VeraBd.ttf", 0.028, folder / "Vera.ttf"
    except ImportError:
        pass
    try:
        from slice.fonts import find_font

        found = find_font(bold=True)
    except Exception:  # noqa: BLE001 - no font file: Pillow's own
        found = None
    return found, 0.028, found


@lru_cache(maxsize=64)
def _font(path: str | None, px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, max(px, 1))
        except OSError:
            pass
    return ImageFont.load_default(max(px, 1))


def heavy(size: float):
    return _font(str(font_files()[0]) if font_files()[0] else None, round(size * SS))


def regular(size: float):
    path = font_files()[2]
    return _font(str(path) if path else None, round(size * SS))


def _stroke(size: float) -> int:
    return round(size * SS * font_files()[1])


def text_width(text: str, size: float, bold: bool = True) -> float:
    """Width in picture pixels of `text` set in the heavy (or regular) font."""
    font = heavy(size) if bold else regular(size)
    extra = 2 * _stroke(size) if bold else 0
    return (font.getlength(text) + extra) / SS


def renderable(text: str) -> bool:
    """True when the font can draw every character (Latin letters, digits and
    common punctuation); a title with anything else is not drawn."""
    return all(ord(ch) < 0x250 or ch in "–—‘’“”…•€" for ch in text)


# ------------------------------------------------------------------ colours


def rgb(colour: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    c = colour.lstrip("#")
    if len(c) == 8 and c.startswith("0x"):
        c = c[2:]
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), round(255 * alpha)


def shade(colour: str, factor: float) -> str:
    r, g, b, _ = rgb(colour)
    return "#{:02x}{:02x}{:02x}".format(*(min(255, round(v * factor)) for v in (r, g, b)))


# ------------------------------------------------------------------ canvas


class Canvas:
    """A picture drawn at SS times its size, with design-pixel coordinates.
    `at()` places a block of drawing (an illustration) at an offset and scale."""

    def __init__(self, size: tuple[int, int], image: Image.Image | None = None):
        self.w, self.h = size
        self.img = image if image is not None else Image.new("RGB", (self.w * SS, self.h * SS), "black")
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self.ox = self.oy = 0.0
        self.k = 1.0

    @contextmanager
    def at(self, ox: float, oy: float, k: float = 1.0):
        old = (self.ox, self.oy, self.k)
        self.ox, self.oy, self.k = ox, oy, k
        try:
            yield self
        finally:
            self.ox, self.oy, self.k = old

    def x(self, v: float) -> float:
        return (self.ox + v * self.k) * SS

    def y(self, v: float) -> float:
        return (self.oy + v * self.k) * SS

    def s(self, v: float) -> float:
        return v * self.k * SS

    def _pts(self, pts):
        return [(self.x(px), self.y(py)) for px, py in pts]

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, width=0, r=0):
        box = [self.x(x0), self.y(y0), self.x(x1), self.y(y1)]
        w = max(1, round(self.s(width))) if outline else 0
        if r:
            self.d.rounded_rectangle(box, radius=self.s(r), fill=fill, outline=outline, width=w)
        else:
            self.d.rectangle(box, fill=fill, outline=outline, width=w)

    def ellipse(self, cx, cy, rx, ry, fill=None, outline=None, width=0):
        box = [self.x(cx - rx), self.y(cy - ry), self.x(cx + rx), self.y(cy + ry)]
        self.d.ellipse(box, fill=fill, outline=outline, width=max(1, round(self.s(width))) if outline else 0)

    def circle(self, cx, cy, r, fill=None, outline=None, width=0):
        self.ellipse(cx, cy, r, r, fill, outline, width)

    def poly(self, pts, fill=None, outline=None):
        self.d.polygon(self._pts(pts), fill=fill, outline=outline)

    def line(self, pts, fill, width, cap=True, closed=False):
        pts = list(pts) + ([pts[0]] if closed else [])
        flat = self._pts(pts)
        w = max(1, round(self.s(width)))
        self.d.line(flat, fill=fill, width=w, joint="curve")
        if cap:
            for px, py in (flat[0], flat[-1]):
                self.d.ellipse([px - w / 2, py - w / 2, px + w / 2, py + w / 2], fill=fill)

    def arc(self, cx, cy, r, a0, a1, fill, width, cap=True):
        self.line(arc_points(cx, cy, r, a0, a1), fill, width, cap)

    def text(self, x, y, txt, font, fill, anchor="ls", stroke=0, tracking=0.0):
        """`tracking` is extra space after each letter, in design pixels."""
        if not tracking:
            self.d.text((self.x(x), self.y(y)), txt, font=font, fill=fill, anchor=anchor,
                        stroke_width=stroke, stroke_fill=fill)
            return
        cur = self.x(x)
        for ch in txt:
            self.d.text((cur, self.y(y)), ch, font=font, fill=fill, anchor="ls")
            cur += font.getlength(ch) + tracking * self.k * SS

    def finish(self) -> Image.Image:
        return self.img.resize((self.w, self.h), Image.LANCZOS)


def arc_points(cx, cy, r, a0, a1, n=48):
    """Points of a circle arc; angles in degrees, 0 = right, growing clockwise
    on screen."""
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def rotate(pts, cx, cy, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c) for x, y in pts]


def bezier(p0, p1, p2, n=16):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / n for i in range(n + 1))]


def arrow_head(c: Canvas, tip, direction_deg, size, fill):
    a = math.radians(direction_deg)
    back = (tip[0] - size * math.cos(a), tip[1] - size * math.sin(a))
    left = (back[0] + size * 0.6 * math.sin(a), back[1] - size * 0.6 * math.cos(a))
    right = (back[0] - size * 0.6 * math.sin(a), back[1] + size * 0.6 * math.cos(a))
    c.poly([tip, left, right], fill=fill)


# -------------------------------------------------------------- backgrounds


def gradient(size: tuple[int, int], kind: str, stops: tuple[str, ...]) -> Image.Image:
    """The card's background, drawn at SS times the size."""
    w, h = size[0] * SS, size[1] * SS
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    if kind == "radial":
        cx, cy = 0.78 * w, 0.45 * h
        t = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / (0.62 * math.hypot(w, h))
    else:  # 135 degrees: top-left to bottom-right
        t = (xs / w + ys / h) / 2
    t = np.clip(t, 0, 1)
    colours = np.array([rgb(s)[:3] for s in stops], dtype=np.float32)
    marks = np.linspace(0, 1, len(stops)) if len(stops) == 2 else np.array([0, 0.55, 1])
    out = np.stack([np.interp(t, marks, colours[:, i]) for i in range(3)], axis=-1)
    return Image.fromarray(out.astype(np.uint8), "RGB")


def cover(image: Image.Image, size: tuple[int, int], focus: float = 0.3) -> Image.Image:
    """The picture scaled to fill `size` and cut to it; `focus` (0 top, 1
    bottom) says which part of a too-tall picture stays."""
    w, h = size
    scale = max(w / image.width, h / image.height)
    new = image.resize((max(w, round(image.width * scale)), max(h, round(image.height * scale))), Image.LANCZOS)
    left = (new.width - w) // 2
    top = round((new.height - h) * focus)
    return new.crop((left, top, left + w, top + h))


# --------------------------------------------------------------- text fitting

Word = tuple[str, bool]  # (word, in the accent colour)


def _cap_h(size: float) -> float:
    return 0.73 * size


def _pitch(size: float) -> float:
    return round(size * 1.04)


def block_height(size: float, count: int) -> float:
    return _cap_h(size) + (count - 1) * _pitch(size)


def _line_width(words: list[Word], size: float) -> float:
    return text_width(" ".join(w for w, _ in words).upper(), size)


def _partitions(words: list[Word], count: int):
    for cuts in itertools.combinations(range(1, len(words)), count - 1):
        edges = (0, *cuts, len(words))
        yield [words[a:b] for a, b in itertools.pairwise(edges)]


def fit_title(words: list[Word], col_w: float, max_h: float, *, max_lines: int, size_max: float,
              size_min: float, lines: list[list[Word]] | None = None) -> tuple[float, list[list[Word]]] | None:
    """(size, lines) for a title: the biggest size from size_max down to
    size_min at which it fits `col_w` wide and `max_h` high, in the given
    `lines` when those fit, else wrapped again into at most `max_lines`
    balanced lines. None when it never fits."""
    if not words:
        return None
    if lines and len(lines) <= max_lines:  # the given line breaks first, as big as they will go
        size = size_max
        while size >= size_min:
            if block_height(size, len(lines)) <= max_h and all(_line_width(line, size) <= col_w for line in lines):
                return size, lines
            size -= 2
    size = size_max
    while size >= size_min:
        for count in range(1, min(max_lines, len(words)) + 1):
            if block_height(size, count) > max_h:
                break
            best = None
            for parts in _partitions(words, count):
                widest = max(_line_width(p, size) for p in parts)
                if widest <= col_w and (best is None or widest < best[0]):
                    best = (widest, parts)
            if best:
                return size, best[1]
        size -= 2
    return None


def auto_accent(lines: list[list[Word]]) -> list[list[Word]]:
    """Mark the key part: the last line when there are two or more, else the
    last word (the last two when the line has four words or more)."""
    plain = [[(w, False) for w, _ in line] for line in lines]
    if len(plain) >= 2:
        plain[-1] = [(w, True) for w, _ in plain[-1]]
    elif plain and plain[0]:
        n = 2 if len(plain[0]) >= 4 else 1
        plain[0] = [(w, i >= len(plain[0]) - n) for i, (w, _) in enumerate(plain[0])]
    return plain


def _draw_title(c: Canvas, x: float, top: float, size: float, lines: list[list[Word]], accent: str) -> float:
    """Draw the title lines (upper case) from `top`; returns the bottom edge."""
    font, stroke = heavy(size), _stroke(size)
    space = text_width(" ", size) * 0.92
    for i, line in enumerate(lines):
        base = top + _cap_h(size) + i * _pitch(size)
        cur = x
        for w, hot in line:
            word = w.upper()
            c.text(cur, base, word, font, rgb(accent if hot else WHITE), stroke=stroke)
            cur += text_width(word, size) + space
    return top + block_height(size, len(lines))


# --------------------------------------------------------------- shared parts


def chip(c: Canvas, text: str, colour: str, *, x=56, y=50, ink=NAVY, box=WHITE, size=26) -> float:
    """The team chip; returns its right edge."""
    text = (text or "").strip().upper()
    if not text:
        return x
    track = size * 0.08
    width = sum(heavy(size).getlength(ch) / SS + track for ch in text) + 36 - track
    height = size + 18
    c.rect(x, y, x + width, y + height, fill=box, r=6)
    c.text(x + 18, y + 9 + size * 0.86, text, heavy(size), rgb(ink), tracking=track)
    return x + width


def brand(c: Canvas, *, left: bool, margin=58, bottom=44, size=26):
    """"FloLabs", Flo in yellow, at the bottom left or right."""
    flo, labs = "Flo", "Labs"
    total = text_width(flo + labs, size)
    x = margin if left else c.w - margin - total
    base = c.h - bottom
    c.text(x, base, flo, heavy(size), rgb(YELLOW))
    c.text(x + text_width(flo, size), base, labs, heavy(size), rgb(WHITE, 0.88))


def accent_bar(c: Canvas, x: float, y: float, colour: str = YELLOW, width: float = 120):
    c.rect(x, y, x + width, y + 12, fill=rgb(colour), r=6)


# -------------------------------------------------------------------- layouts


def _layout(size: tuple[int, int]):
    """Where things go: the title column (left, width, top, bottom) and the
    box for the illustration (x, y, scale)."""
    w, h = size
    if w >= h:
        return {"left": 56, "col_w": 650, "top": 120, "bottom": h - 80, "art": (w - 20 - ART_BOX[0], 0, 1.0),
                "chip_y": 50, "unit": 1.0}
    unit = 1.45  # a Short's thumbnail is small on a phone: everything is bigger
    return {"left": 70, "col_w": w - 140, "top": round(h * 0.60), "bottom": h - 120,
            "art": ((w - ART_BOX[0] * 1.2) / 2, round(h * 0.09), 1.2), "chip_y": 70, "unit": unit}


def render_card(spec: dict, size: tuple[int, int] = (1280, 720)) -> Image.Image:
    """A designed card. spec: template, art, palette, lines (list of str),
    accent_line (index or None), subtitle, chip, chip_colour. Raises ValueError
    when the title is empty or cannot be fitted."""
    template = spec.get("template") if spec.get("template") in TEMPLATES else "statement"
    art_default, palette_default = TEMPLATE_DEFAULTS[template]
    art = spec.get("art") if spec.get("art") in ART_IDS else art_default
    palette = spec.get("palette") if spec.get("palette") in PALETTES else palette_default
    kind, stops, accent = PALETTES[palette]
    lines_in = [str(x).strip() for x in (spec.get("lines") or []) if str(x).strip()][:3]
    if not lines_in:
        raise ValueError("a card needs a title")
    if not all(renderable(x) for x in lines_in):
        raise ValueError("the title has characters the font cannot draw")
    hot_line = spec.get("accent_line")
    lines: list[list[Word]] = [[(w, i == hot_line) for w in line.split()] for i, line in enumerate(lines_in)]
    words = [w for line in lines for w in line]
    lay = _layout(size)
    unit = lay["unit"]
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle and not renderable(subtitle):
        subtitle = ""
    sub_size = 40 * unit
    if subtitle:
        # one line, shrunk to a floor to fit the title column; dropped if it still does not fit
        floor = 28 * unit
        while sub_size > floor and regular(sub_size).getlength(subtitle) / SS > lay["col_w"]:
            sub_size -= 1
        if regular(sub_size).getlength(subtitle) / SS > lay["col_w"]:
            subtitle = ""
            sub_size = 40 * unit
    sub_room = (sub_size + 26 * unit) if subtitle else 0
    avail = lay["bottom"] - lay["top"] - 36 * unit - sub_room
    fitted = fit_title(words, lay["col_w"], avail, max_lines=3 if size[0] >= size[1] else 4,
                       size_max=108 * unit, size_min=44 * unit, lines=lines)
    if fitted is None:
        raise ValueError("the title does not fit the card")
    tsize, tlines = fitted
    if hot_line is None or not any(hot for line in tlines for _, hot in line):
        tlines = auto_accent(tlines)
    c = Canvas(size, gradient(size, kind, stops))
    with c.at(lay["art"][0], lay["art"][1], lay["art"][2]):
        ART[art](c, accent)
    chip(c, spec.get("chip") or "", spec.get("chip_colour") or "14213d", y=lay["chip_y"], size=26 * unit)
    block = 12 + 24 * unit + block_height(tsize, len(tlines)) + sub_room
    zone_top, zone_bottom = lay["top"], lay["bottom"]
    top = zone_top + max(0, (zone_bottom - zone_top - block) * 0.45)
    accent_bar(c, lay["left"], top, YELLOW)
    title_top = top + 12 + 24 * unit
    bottom = _draw_title(c, lay["left"], title_top, tsize, tlines, accent)
    if subtitle:
        c.text(lay["left"] + 2, bottom + 26 * unit + sub_size * 0.74, subtitle, regular(sub_size), rgb("#dbe3f5"))
    brand(c, left=True, bottom=44 if size[0] >= size[1] else 70, size=26 * unit)
    return c.finish()


def chip_size(text: str, size: float) -> tuple[float, float]:
    """(width, height) of the team chip, 0 wide when there is no text."""
    text = (text or "").strip().upper()
    if not text:
        return 0.0, 0.0
    track = size * 0.08
    return sum(heavy(size).getlength(ch) / SS + track for ch in text) + 36 - track, size + 18


def frame_layout(size: tuple[int, int], label: str, chip_text: str, fitted=None) -> dict:
    """Boxes (x0, y0, x1, y1) for a real-picture thumbnail: the dark band, the
    team chip, the yellow accent bar and the label. The chip sits in the band
    just above the accent bar, left-aligned with the label, so nothing is drawn
    over the top of the picture; the band grows to hold it."""
    w, h = size
    unit = 1.0 if w >= h else 1.45
    left = 56 if w >= h else 70
    base_y = h - 92 * (1 if w >= h else 1.3)
    cw, ch = chip_size(chip_text, 26 * unit)
    gap = 16 * unit
    band_h = round(h * 0.46)
    out = {"unit": unit, "left": left, "base_y": base_y, "label": None, "bar": None}
    if fitted:
        tsize, tlines = fitted
        title_top = base_y - block_height(tsize, len(tlines))
        bar_y = title_top - 36 * unit
        out["label"] = (left, title_top, left + w - 2 * left, base_y)
        out["bar"] = (left, bar_y, left + 120, bar_y + 12)
        out["title_top"] = title_top
        chip_y = bar_y - gap - ch
    else:
        chip_y = base_y - ch
    out["chip"] = (left, chip_y, left + cw, chip_y + ch) if cw else None
    if cw and fitted:
        # the shade is clear for its top 16%: keep the chip below that
        band_h = max(band_h, min(h, math.ceil((h - chip_y + 24 * unit) / 0.84)))
    out["band"] = (0, h - band_h, w, h)
    return out


def render_frame(base: Image.Image, label: str, chip_text: str, chip_colour: str,
                 size: tuple[int, int] = (1280, 720)) -> Image.Image:
    """A real picture with the label over a dark band. Raises ValueError when
    the label cannot be fitted in two lines (three on a Short); an empty label draws no band."""
    w, h = size
    img = cover(base.convert("RGB"), size).resize((w * SS, h * SS), Image.LANCZOS)
    words = [(x, False) for x in (label or "").split()]
    if words and not renderable(label):
        raise ValueError("the label has characters the font cannot draw")
    unit = 1.0 if w >= h else 1.45
    col_w = w - 112 if w >= h else w - 140
    fitted = None
    if words:
        fitted = fit_title(words, col_w, 420 * unit, max_lines=2 if w >= h else 3, size_max=96 * unit,
                           size_min=44 * unit)
        if fitted is None:
            raise ValueError("the label does not fit")
    lay = frame_layout(size, label, chip_text, fitted)
    left = lay["left"]
    # the band: clear at the top, solid from about a third of the way down
    band_h = h - lay["band"][1]
    ys = np.arange(h * SS - band_h * SS, h * SS, dtype=np.float32) - (h * SS - band_h * SS)
    alpha = np.interp(ys / (band_h * SS), [0, 0.16, 0.3, 1], [0, 0.9, 1, 1])
    shade_img = Image.new("RGB", (w * SS, band_h * SS), FRAME_SHADE)
    mask = Image.fromarray((np.repeat(alpha[:, None], w * SS, axis=1) * 255).astype(np.uint8), "L")
    img.paste(shade_img, (0, (h - band_h) * SS), mask)
    c = Canvas(size, img)
    if fitted:
        tsize, tlines = fitted
        tlines = auto_accent(tlines)
        accent_bar(c, left, lay["bar"][1])
        _draw_title(c, left, lay["title_top"], tsize, tlines, YELLOW)
    if lay["chip"]:
        chip(c, chip_text, chip_colour, x=left, y=lay["chip"][1], ink=WHITE,
             box="#" + chip_colour.lstrip("#"), size=26 * unit)
    brand(c, left=False, bottom=44 if w >= h else 70, size=26 * unit)
    return c.finish()


# ------------------------------------------------------------ the illustrations
# Each draws in a 560x720 box, around (290, 360), with `accent` for the key colour.


def _stars(c, pts, r=2.5, alpha=0.7):
    for x, y in pts:
        c.circle(x, y, r, fill=rgb(WHITE, alpha))


def art_network(c, accent):
    for rx, ry in ((250, 90), (180, 64), (110, 40)):
        pts = rotate([(290 + rx * math.cos(t / 40 * math.tau), 360 + ry * math.sin(t / 40 * math.tau))
                      for t in range(41)], 290, 360, -18)
        c.line(pts, rgb("#7b6cff", 0.35), 2, cap=False)
    edges = [((120, 250), (260, 330)), ((260, 330), (430, 260)), ((260, 330), (340, 470)), ((340, 470), (500, 430)),
             ((430, 260), (500, 430)), ((120, 250), (180, 480)), ((180, 480), (340, 470))]
    for a, b in edges:
        c.line([a, b], rgb(YELLOW, 0.92), 3)
    for (x, y), r in (((120, 250), 11), ((260, 330), 16), ((430, 260), 12), ((340, 470), 13), ((500, 430), 10),
                      ((180, 480), 9)):
        c.circle(x, y, r, fill=rgb(WHITE))
    c.circle(260, 330, 34, outline=rgb(YELLOW), width=4)
    _stars(c, [(50, 120), (520, 110), (540, 600), (80, 620), (380, 80), (210, 640), (550, 300)])


def art_doc_to_code(c, accent):
    c.rect(70, 190, 240, 410, fill=rgb(WHITE), r=16)
    for i, w in enumerate((114, 90, 104, 70)):
        c.rect(98, 230 + 32 * i, 98 + w, 244 + 32 * i, fill=rgb(GREY), r=7)
    c.rect(98, 380, 98 + 90, 394, fill=rgb(GREY), r=7)
    c.line([(262, 300), (332, 300)], rgb(YELLOW), 16)
    c.line([(318, 268), (358, 300), (318, 332)], rgb(YELLOW), 16)
    c.rect(380, 190, 550, 410, fill=rgb(NAVY), r=16)
    c.text(465, 322, "</>", heavy(60), rgb(YELLOW), anchor="ms")
    c.circle(470, 520, 46, outline=rgb(YELLOW, 0.9), width=8)
    c.line([(470, 492), (470, 520), (488, 534)], rgb(YELLOW, 0.9), 8)


def art_chip_mic(c, accent):
    c.rect(150, 220, 430, 500, fill=rgb(NAVY), r=30)
    c.rect(200, 270, 380, 450, fill=rgb(NAVY2), outline=rgb(YELLOW), width=5, r=16)
    for i in range(4):
        x = 195 + 60 * i
        c.line([(x, 200), (x, 220)], rgb(YELLOW), 10)
        c.line([(x, 500), (x, 520)], rgb(YELLOW), 10)
        y = 270 + 60 * i
        c.line([(130, y), (150, y)], rgb(YELLOW), 10)
        c.line([(430, y), (450, y)], rgb(YELLOW), 10)
    c.rect(270, 300, 310, 380, fill=rgb(WHITE), r=20)
    c.arc(290, 360, 38, 0, 180, rgb(WHITE), 8)
    c.line([(290, 398), (290, 420)], rgb(WHITE), 8)
    c.arc(400, 360, 70, -60, 60, rgb(WHITE, 0.8), 8)
    c.arc(400, 360, 110, -55, 55, rgb(WHITE, 0.6), 8)


def art_robot_loop(c, accent):
    top, bottom = TEAL, YELLOW
    c.arc(290, 350, 150, 195, 318, rgb(top), 22)
    c.arc(290, 350, 150, 15, 138, rgb(bottom), 22)
    e = (290 + 150 * math.cos(math.radians(318)), 350 + 150 * math.sin(math.radians(318)))
    arrow_head(c, (e[0] + 28, e[1] + 26), 52, 54, rgb(top))
    e2 = (290 + 150 * math.cos(math.radians(138)), 350 + 150 * math.sin(math.radians(138)))
    arrow_head(c, (e2[0] - 28, e2[1] - 26), 232, 54, rgb(bottom))
    c.rect(235, 300, 345, 400, fill=rgb(WHITE), r=18)
    c.circle(268, 345, 10, fill=rgb(NAVY))
    c.circle(312, 345, 10, fill=rgb(NAVY))
    c.rect(262, 370, 318, 379, fill=rgb(NAVY), r=4)
    c.line([(290, 300), (290, 276)], rgb(WHITE), 8)
    c.circle(290, 270, 9, fill=rgb(WHITE))
    for i, w in enumerate((160, 120, 140)):
        c.rect(70, 560 + 30 * i, 70 + w, 574 + 30 * i, fill=rgb("#c9d1e3"), r=7)


def art_robot_arm(c, accent):
    c.rect(130, 590, 450, 630, fill=rgb(NAVY), outline=rgb(YELLOW), width=4, r=10)
    c.rect(250, 540, 330, 592, fill=rgb(NAVY2), r=8)
    c.circle(290, 540, 34, fill=rgb(YELLOW))
    c.line([(290, 540), (200, 380)], rgb(WHITE), 40)
    c.circle(200, 380, 28, fill=rgb(YELLOW))
    c.circle(200, 380, 10, fill=rgb(NAVY))
    c.line([(200, 380), (350, 250)], rgb(WHITE), 34)
    c.circle(350, 250, 24, fill=rgb(YELLOW))
    c.circle(350, 250, 8, fill=rgb(NAVY))
    c.line([(372, 240), (420, 270), (432, 322)], rgb(YELLOW), 14)
    c.line([(372, 232), (440, 232), (472, 282)], rgb(YELLOW), 14)
    c.rect(438, 306, 480, 348, fill=rgb(WHITE), r=6)
    c.arc(290, 540, 230, 210, 250, rgb(WHITE, 0.45), 8)
    _stars(c, [(70, 160), (480, 130), (500, 520)], 3)


def _rocket_body(c, accent):
    nose = bezier((-40, -40), (-34, -140), (0, -190))
    nose2 = bezier((0, -190), (34, -140), (40, -40))
    body = [(40 + 0, 100), (-40, 100)]
    pts = [(x + 290, y + 340) for x, y in nose + nose2 + body]
    # fins
    c.poly([(290 - 40, 340 + 20), (290 - 100, 340 + 120), (290 - 40, 340 + 100)], fill=rgb(YELLOW))
    c.poly([(290 + 40, 340 + 20), (290 + 100, 340 + 120), (290 + 40, 340 + 100)], fill=rgb(YELLOW))
    c.poly([(290 - 24, 340 + 100), (290 + 24, 340 + 100), (290, 340 + 190), ], fill=rgb(YELLOW, 0.95))
    c.poly([(290 - 12, 340 + 100), (290 + 12, 340 + 100), (290, 340 + 150)], fill=rgb(WHITE))
    c.poly(pts, fill=rgb(WHITE))
    c.rect(290 - 40, 340 + 60, 290 + 40, 340 + 80, fill=rgb(accent))
    c.circle(290, 340 - 40, 26, fill=rgb(NAVY), outline=rgb(accent), width=6)
    c.circle(282, 340 - 48, 7, fill=rgb(WHITE, 0.7))


def _layer() -> Canvas:
    """A transparent 560x720 canvas, for a drawing that is then turned."""
    layer = Canvas(ART_BOX, Image.new("RGBA", (ART_BOX[0] * SS, ART_BOX[1] * SS), (0, 0, 0, 0)))
    return layer


def art_rocket(c, accent):
    layer = _layer()
    _rocket_body(layer, accent)
    _paste(c, layer.img.rotate(-38, resample=Image.BICUBIC, center=(290 * SS, 340 * SS)))
    c.arc(290, 380, 230, 150, 215, rgb(WHITE, 0.35), 4)
    _stars(c, [(70, 120), (500, 100), (520, 560), (90, 600), (400, 640), (150, 330)], 3)


def art_satellite(c, accent):
    layer = _layer()
    layer.rect(250, 300, 330, 380, fill=rgb(WHITE), r=12)
    layer.rect(266, 316, 314, 364, fill=rgb(NAVY), r=6)
    for sx in (-1, 1):
        x0 = 290 + sx * 50
        panel = (x0, 310, x0 + sx * 130, 370)
        layer.rect(min(panel[0], panel[2]), 310, max(panel[0], panel[2]), 370, fill=rgb(accent), r=4)
        for i in range(1, 4):
            xx = x0 + sx * 130 * i / 4
            layer.line([(xx, 310), (xx, 370)], rgb(NAVY), 3, cap=False)
        layer.line([(min(panel[0], panel[2]), 340), (max(panel[0], panel[2]), 340)], rgb(NAVY), 3, cap=False)
    layer.line([(290, 300), (290, 270)], rgb(WHITE), 6)
    layer.arc(290, 262, 34, 180, 360, rgb(WHITE), 8)
    layer.circle(290, 250, 7, fill=rgb(accent))
    turned = layer.img.rotate(-30, resample=Image.BICUBIC, center=(290 * SS, 340 * SS))
    _paste(c, turned)
    for r, alpha in ((60, 0.9), (95, 0.6), (130, 0.35)):
        c.arc(370, 260, r, -80, -10, rgb(WHITE, alpha), 7)
    c.circle(290, 650, 200, fill=rgb("#13a39b", 0.35))
    _stars(c, [(70, 120), (500, 90), (60, 480), (520, 540)], 3)


def _paste(c: Canvas, layer: Image.Image):
    """Paste a transparent layer drawn in the 560x720 box (at SS) at the
    canvas's current offset and scale."""
    if abs(c.k - 1.0) > 1e-6:
        layer = layer.resize((round(layer.width * c.k), round(layer.height * c.k)), Image.LANCZOS)
    c.img.paste(layer, (round(c.ox * SS), round(c.oy * SS)), layer)
    c.d = ImageDraw.Draw(c.img, "RGBA")


def art_brain(c, accent):
    blobs = [(210, 330, 70), (250, 250, 70), (330, 250, 70), (375, 330, 70), (230, 420, 65), (350, 420, 65),
             (290, 340, 80)]
    for x, y, r in blobs:
        c.circle(x, y, r, fill=rgb(WHITE))
    c.rect(270, 470, 310, 520, fill=rgb(WHITE), r=10)
    c.line([(290, 200), (290, 470)], rgb(NAVY), 6)
    for pts in ([(210, 300), (250, 320), (230, 370)], [(370, 300), (330, 320), (350, 370)],
                [(240, 240), (262, 270), (290, 262)], [(340, 240), (318, 270), (290, 262)],
                [(225, 430), (255, 410), (270, 440)], [(355, 430), (325, 410), (310, 440)]):
        c.line(pts, rgb(NAVY), 6)
    nodes = [(250, 290), (330, 290), (250, 390), (330, 390), (290, 340)]
    for a, b in ((0, 4), (1, 4), (2, 4), (3, 4)):
        c.line([nodes[a], nodes[b]], rgb(accent), 5)
    for x, y in nodes:
        c.circle(x, y, 11, fill=rgb(accent), outline=rgb(NAVY), width=3)
    for x, y in ((100, 200), (480, 210), (90, 520), (490, 500)):
        c.circle(x, y, 8, fill=rgb(accent, 0.8))
    c.line([(100, 200), (210, 270)], rgb(accent, 0.5), 3)
    c.line([(480, 210), (380, 270)], rgb(accent, 0.5), 3)


def art_heart(c, accent):
    pts = []
    for i in range(121):
        t = i / 120 * math.tau
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((290 + 12.5 * x, 330 - 12.5 * y))
    c.poly(pts, fill=rgb(CORAL))
    c.line([(110, 400), (220, 400), (250, 330), (290, 470), (330, 360), (360, 400), (480, 400)], rgb(WHITE), 12)
    c.circle(480, 400, 10, fill=rgb(YELLOW))
    for x, y in ((90, 180), (480, 170), (500, 560), (80, 560)):
        c.circle(x, y, 9, fill=rgb(YELLOW, 0.8))
    c.arc(290, 330, 230, 200, 340, rgb(WHITE, 0.25), 5)


def art_gear(c, accent):
    def gear(cx, cy, R, r, n, fill):
        pts = []
        for i in range(n):
            a = i * math.tau / n
            step = math.tau / n
            for off, rad in ((-0.32, r), (-0.2, R), (0.2, R), (0.32, r)):
                pts.append((cx + rad * math.cos(a + off * step * 1.5), cy + rad * math.sin(a + off * step * 1.5)))
        c.poly(pts, fill=fill)

    gear(270, 360, 175, 140, 10, rgb(WHITE))
    c.circle(270, 360, 80, fill=rgb(NAVY))
    c.circle(270, 360, 80, outline=rgb(accent), width=10)
    c.circle(270, 360, 26, fill=rgb(accent))
    gear(450, 215, 80, 62, 8, rgb(accent))
    c.circle(450, 215, 30, fill=rgb(NAVY))
    gear(440, 520, 60, 46, 8, rgb(GREY))
    c.circle(440, 520, 20, fill=rgb(NAVY))


def art_database(c, accent):
    for i, y in enumerate((250, 360, 470)):
        c.rect(160, y, 420, y + 90, fill=rgb(NAVY2))
        c.ellipse(290, y + 90, 130, 36, fill=rgb(NAVY2))
        c.ellipse(290, y, 130, 36, fill=rgb(WHITE if i == 0 else GREY))
        c.line([(160, y), (160, y + 90)], rgb(WHITE, 0.9), 4, cap=False)
        c.line([(420, y), (420, y + 90)], rgb(WHITE, 0.9), 4, cap=False)
        c.circle(380, y + 50, 8, fill=rgb(accent))
        c.circle(350, y + 50, 8, fill=rgb(WHITE, 0.6))
        c.ellipse(290, y + 90, 130, 36, outline=rgb(WHITE, 0.9), width=4)
    c.ellipse(290, 250, 130, 36, outline=rgb(accent), width=6)


def art_chart(c, accent):
    c.rect(100, 190, 500, 540, fill=rgb(NAVY2), outline=rgb(WHITE, 0.25), width=3, r=22)
    c.line([(150, 490), (460, 490)], rgb(WHITE, 0.8), 5)
    c.line([(150, 230), (150, 490)], rgb(WHITE, 0.8), 5)
    for i, hgt in enumerate((70, 110, 95, 170, 230)):
        x = 185 + 55 * i
        c.rect(x, 490 - hgt, x + 38, 486, fill=rgb(accent if i == 4 else WHITE), r=6)
    pts = [(190, 400), (250, 360), (305, 375), (360, 310), (440, 235)]
    c.line(pts, rgb(YELLOW if accent != YELLOW else CORAL), 9)
    arrow_head(c, (462, 218), -40, 38, rgb(YELLOW if accent != YELLOW else CORAL))


def art_camera(c, accent):
    c.rect(110, 270, 480, 520, fill=rgb(WHITE), r=34)
    c.rect(200, 235, 310, 280, fill=rgb(WHITE), r=14)
    c.circle(295, 395, 100, fill=rgb(NAVY))
    c.circle(295, 395, 78, fill=rgb(NAVY2), outline=rgb(accent), width=8)
    c.circle(295, 395, 40, fill=rgb(NAVY), outline=rgb(WHITE, 0.5), width=4)
    c.circle(270, 370, 12, fill=rgb(WHITE, 0.8))
    c.circle(430, 310, 12, fill=rgb(accent))
    c.rect(140, 300, 190, 320, fill=rgb(GREY), r=6)
    for sx, sy in ((60, 150), (500, 150), (60, 580), (500, 580)):
        dx, dy = (1 if sx < 280 else -1), (1 if sy < 360 else -1)
        c.line([(sx, sy + dy * 50), (sx, sy), (sx + dx * 50, sy)], rgb(accent), 8)


def art_wifi(c, accent):
    for i, r in enumerate((60, 110, 160)):
        c.arc(290, 400, r, 228, 312, rgb(WHITE if i % 2 else accent), 18)
    c.circle(290, 400, 18, fill=rgb(accent))
    c.rect(180, 470, 400, 600, fill=rgb(NAVY), outline=rgb(accent), width=5, r=18)
    c.rect(230, 505, 300, 565, fill=rgb(NAVY2), outline=rgb(WHITE, 0.6), width=3, r=8)
    c.circle(350, 535, 11, fill=rgb(accent))
    c.circle(350, 535, 24, outline=rgb(accent, 0.5), width=3)
    for x in (205, 235, 265, 295, 325, 355):
        c.line([(x, 600), (x, 620)], rgb(accent), 6)
    c.line([(290, 418), (290, 470)], rgb(WHITE), 8)


def art_cloud(c, accent):
    for cx, cy, r in ((220, 380, 70), (300, 320, 95), (395, 365, 72)):
        c.circle(cx, cy, r, fill=rgb(WHITE))
    c.rect(200, 370, 440, 450, fill=rgb(WHITE), r=40)
    c.line([(290, 420), (290, 340)], rgb(accent), 16)
    c.line([(252, 372), (290, 332), (328, 372)], rgb(accent), 16)
    for x, y in ((120, 560), (290, 590), (460, 560)):
        c.rect(x - 40, y - 24, x + 40, y + 24, fill=rgb(NAVY2), outline=rgb(WHITE, 0.7), width=3, r=8)
        c.circle(x + 20, y, 6, fill=rgb(accent))
        c.line([(x, y - 24), (290, 450)], rgb(accent, 0.55), 4)


def art_globe(c, accent):
    c.circle(290, 360, 170, fill=rgb(NAVY2), outline=rgb(WHITE), width=8)
    for rx in (60, 120):
        c.ellipse(290, 360, rx, 170, outline=rgb(WHITE, 0.55), width=4)
    c.line([(290, 190), (290, 530)], rgb(WHITE, 0.55), 4, cap=False)
    for dy, half in ((-85, 147), (0, 170), (85, 147)):
        c.line([(290 - half, 360 + dy), (290 + half, 360 + dy)], rgb(WHITE, 0.55), 4, cap=False)
    orbit = rotate([(290 + 240 * math.cos(t / 60 * math.tau), 360 + 70 * math.sin(t / 60 * math.tau))
                    for t in range(61)], 290, 360, -24)
    c.line(orbit, rgb(accent), 6, cap=False)
    c.circle(orbit[44][0], orbit[44][1], 16, fill=rgb(WHITE), outline=rgb(accent), width=5)
    for x, y in ((235, 300), (330, 340), (270, 420)):
        c.circle(x, y, 9, fill=rgb(accent))


ART = {
    "network": art_network, "doc_to_code": art_doc_to_code, "chip_mic": art_chip_mic, "robot_loop": art_robot_loop,
    "robot_arm": art_robot_arm, "rocket": art_rocket, "satellite": art_satellite, "brain": art_brain,
    "heart": art_heart, "gear": art_gear, "database": art_database, "chart": art_chart, "camera": art_camera,
    "wifi": art_wifi, "cloud": art_cloud, "globe": art_globe,
}
assert set(ART) == set(ART_IDS)

# words in a title that suggest an illustration, in order of priority
ART_WORDS = (
    ("robot_arm", ("arm", "gripper", "manipulator", "actuator", "servo")),
    ("rocket", ("rocket", "launch", "space", "orbit", "mission")),
    ("satellite", ("satellite", "telemetry", "antenna", "radio", "signal")),
    ("heart", ("mood", "health", "heart", "wellbeing", "emotion", "feeling")),
    ("brain", ("brain", "learning", "model", "neural", "reasoning", "memory", "agent")),
    ("gear", ("process", "automation", "workflow", "pipeline", "engine", "settings")),
    ("database", ("database", "storage", "sql", "redis", "cache", "queue", "session")),
    ("chart", ("metrics", "growth", "analytics", "dashboard", "revenue", "results", "numbers", "views")),
    ("camera", ("camera", "vision", "image", "video", "detection", "recognition")),
    ("wifi", ("sensor", "iot", "wifi", "esp32", "bluetooth", "wireless", "network")),
    ("cloud", ("cloud", "deploy", "server", "hosting", "aws", "docker")),
    ("globe", ("global", "world", "map", "maps", "universe", "platform", "web")),
    ("chip_mic", ("voice", "audio", "chip", "hardware", "firmware", "microphone", "speech")),
    ("robot_loop", ("improve", "improves", "loop", "iteration", "feedback", "self")),
    ("doc_to_code", ("code", "coding", "prd", "spec", "docs", "document", "developer", "software")),
)


def pick_art(text: str, seed: str = "") -> str:
    """The illustration that fits the words, else a steady pick from the seed."""
    words = {w.strip(".,:;!?()'\"").lower() for w in (text or "").split()}
    for art, keys in ART_WORDS:
        if words & set(keys):
            return art
    return ART_IDS[sum(ord(ch) for ch in (seed or text or "x")) % len(ART_IDS)]


def template_for(art: str) -> str:
    return {"network": "diagram", "globe": "diagram", "chart": "diagram", "database": "diagram", "cloud": "diagram",
            "chip_mic": "hardware", "wifi": "hardware", "gear": "hardware", "robot_arm": "hardware",
            "camera": "hardware"}.get(art, "concept" if art in ("robot_loop", "brain", "heart", "rocket", "satellite")
                                      else "statement")
