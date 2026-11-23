"""The closing card of a new-style Short: "Watch the full meeting" on the
FloLabs navy/teal/yellow look, the meeting's title, optionally a picture of the
meeting as a video tile with a play icon, and an arrow pointing down to the
related video link. One PNG, drawn at 1080x1920 design pixels and scaled."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from publish import thumbart as art

CARD_SECONDS = 4.5
HEADLINE = "Watch the full meeting"
CUE = "Link in the related video below"
_W, _H = 1080, 1920


def _wrap(text: str, size: float, max_w: float, max_lines: int, bold: bool = True) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and art.text_width(trial, size, bold) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        kept[-1] = kept[-1].rstrip(" .,:;") + "…"
        return kept
    return lines


def _picture(path: Path | None) -> Image.Image | None:
    if path is None:
        return None
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except (OSError, ValueError):
        return None


def render_end_card(dest: Path, title: str, size: tuple[int, int] = (_W, _H), picture: Path | None = None) -> Path:
    """Draw the card to `dest` (a PNG of `size`). Text only when `picture` is
    missing or unreadable."""
    w, h = size
    pic = _picture(picture)
    kind, stops, _accent = art.PALETTES["navy"]
    base = art.gradient((_W, _H), kind, stops)
    c = art.Canvas((_W, _H), base)

    title = (title or "").strip()
    if not art.renderable(title):
        title = ""
    head = _wrap(HEADLINE, 112, 900, 2)
    tlines = _wrap(title, 52, 900, 2, bold=False) if title else []

    head_h = len(head) * 130
    title_h = len(tlines) * 66 + (40 if tlines else 0)
    tile_h = 506 + 70 if pic is not None else 0
    cue_h = 190
    block = head_h + title_h + tile_h + cue_h
    top = max(460, (_H - block) // 2 - 40)

    y = top
    c.rect(_W / 2 - 60, y - 60, _W / 2 + 60, y - 48, fill=art.rgb(art.YELLOW), r=6)
    for k, line in enumerate(head):
        col = art.rgb(art.YELLOW) if k == len(head) - 1 else art.rgb(art.WHITE)
        c.text(_W / 2, y + 100 + k * 130, line, art.heavy(112), col, anchor="ms")
    y += head_h
    if tlines:
        y += 40
        for k, line in enumerate(tlines):
            c.text(_W / 2, y + 48 + k * 66, line, art.regular(52), art.rgb(art.GREY), anchor="ms")
        y += len(tlines) * 66
    if pic is not None:
        y += 70
        tw, th = 900, 506
        x0 = (_W - tw) / 2
        tile = art.cover(pic, (tw * art.SS, th * art.SS), 0.3)
        mask = Image.new("L", tile.size, 0)
        from PIL import ImageDraw
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, tile.width - 1, tile.height - 1], radius=28 * art.SS, fill=255)
        c.img.paste(tile, (round(x0 * art.SS), round(y * art.SS)), mask)
        c.rect(x0, y, x0 + tw, y + th, outline=art.rgb(art.TEAL), width=6, r=28)
        cx, cy = _W / 2, y + th / 2
        c.circle(cx, cy, 70, fill=art.rgb(art.NAVY, 0.78))
        c.circle(cx, cy, 70, outline=art.rgb(art.WHITE), width=5)
        c.poly([(cx - 20, cy - 34), (cx - 20, cy + 34), (cx + 38, cy)], fill=art.rgb(art.YELLOW))
        y += th
    # the arrow, pointing down at the link
    y += 70
    ax = _W / 2
    c.line([(ax, y), (ax, y + 70)], art.rgb(art.YELLOW), 14)
    c.poly([(ax - 44, y + 52), (ax + 44, y + 52), (ax, y + 110)], fill=art.rgb(art.YELLOW))
    c.text(_W / 2, y + 190, CUE, art.heavy(44), art.rgb(art.WHITE), anchor="ms")
    art.brand(c, left=False, margin=58, bottom=120, size=34)

    img = c.finish()
    if (w, h) != (_W, _H):
        img = img.resize((w, h), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return dest
