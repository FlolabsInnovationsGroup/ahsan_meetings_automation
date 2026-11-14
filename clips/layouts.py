"""Picture layouts: how a 16:9 (or any) frame becomes a 1080x1920 Short.

Geometry is written for 1080x1920 and scaled when another output size is asked
for. Text lives inside the area the Shorts player leaves free (nothing above
y=288 or below y=1248, nothing right of x=888), so the picture panels sit
where the title bar and captions (see clips.captions) do not cover them.

`video_filter` returns a filtergraph that reads the input as [0:v] and writes
its result to the label [v]; the renderer adds the subtitles after that label.
"""

from __future__ import annotations

from clips.models import Layout, Moment, Source

BASE_W, BASE_H = 1080, 1920
SLICE_MIN_HEIGHT = 1440  # a 9:16 slice from a smaller frame would be upscaled too far and look soft
SQUARE_TOP = 520  # the gameplay square starts below the title bar (y 300-500)
FULL_TOP = 540
FACE_H = 640  # the face camera part of a stack
GAME_H = 1280  # the gameplay part of a stack
BLUR_DOWN = 4  # the background is blurred at a quarter of its size: cheaper, and it looks the same


def even(value: float, floor: int = 2) -> int:
    """Round down to an even number (yuv420p needs even sizes)."""
    return max(floor, int(value) // 2 * 2)


def resolve_layout(layout: Layout, facecam_box, width: int, height: int) -> Layout:
    """Turn any layout, AUTO included, into one that can really be drawn."""
    if layout is Layout.AUTO:
        layout = Layout.FACECAM_STACK if facecam_box else Layout.SQUARE
    if layout is Layout.FACECAM_STACK and not facecam_box:
        return Layout.SQUARE
    if layout is Layout.CENTER_SLICE and height < SLICE_MIN_HEIGHT:
        return Layout.SQUARE
    return layout


def choose_layout(moment: Moment, source: Source, width: int, height: int) -> Layout:
    """The layout for a moment: its own choice if it made one, else by the
    rules (a face camera box means a stack, otherwise the square). Never AUTO."""
    return resolve_layout(moment.layout, source.facecam_box if source else None, width, height)


def _background(k: float, out_w: int, out_h: int) -> str:
    """Branch from the split input: a dark, blurred, zoomed copy of the frame."""
    small_w, small_h = even(out_w / BLUR_DOWN), even(out_h / BLUR_DOWN)
    return (
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
        f"scale={small_w}:{small_h},boxblur=8:2,scale={out_w}:{out_h},"
        "eq=brightness=-0.3:saturation=0.7[b]"
    )


def _on_background(panel: str, y: int) -> str:
    return f"{panel};[b][g]overlay=0:{y},setsar=1,format=yuv420p[v]"


def _square(width: int, height: int, k: float, out_w: int, out_h: int) -> str:
    side = even(min(width, height))
    panel = f"[fg]crop={side}:{side}:(iw-{side})/2:(ih-{side})/2,scale={out_w}:{out_w}[g]"
    return _graph(_background(k, out_w, out_h), _on_background(panel, round(SQUARE_TOP * k)))


def _full_width(width: int, height: int, k: float, out_w: int, out_h: int) -> str:
    panel = f"[fg]scale={out_w}:-2[g]"
    return _graph(_background(k, out_w, out_h), _on_background(panel, round(FULL_TOP * k)))


def _center_slice(width: int, height: int, k: float, out_w: int, out_h: int) -> str:
    cw = even(min(width, height * out_w / out_h))
    return (
        f"[0:v]crop={cw}:{even(height)}:(iw-{cw})/2:0,scale={out_w}:{out_h},setsar=1,format=yuv420p[v]"
    )


def _graph(background: str, rest: str) -> str:
    return f"[0:v]split=2[bg][fg];{background};{rest}"


def _facecam_stack(width: int, height: int, k: float, out_w: int, out_h: int, box) -> str:
    bx, by, bw, bh = box
    fx, fy = even(bx * width, 0), even(by * height, 0)
    fw, fh = even(bw * width), even(bh * height)
    fw, fh = min(fw, width - fx), min(fh, height - fy)
    top_h, game_h = round(FACE_H * k), round(GAME_H * k)
    # gameplay: the part of the frame with the stack's aspect, moved clear of the face box
    gw = even(min(width, height * out_w / game_h))
    gx = (width - gw) // 2
    if gx < fx + fw and gx + gw > fx:  # overlaps the face box: slide to the side away from it
        gx = width - gw if fx + fw / 2 < width / 2 else 0
    face = (
        f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={out_w}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{top_h}[f]"
    )
    game = f"[gm]crop={gw}:{even(height)}:{gx}:0,scale={out_w}:{game_h}[g]"
    return f"[0:v]split=2[fc][gm];{face};{game};[f][g]vstack=inputs=2,setsar=1,format=yuv420p[v]"


def video_filter(
    layout: Layout,
    width: int,
    height: int,
    *,
    facecam_box=None,
    out_w: int = BASE_W,
    out_h: int = BASE_H,
) -> str:
    """The ffmpeg filtergraph for one layout. Reads [0:v], writes [v]."""
    layout = resolve_layout(layout, facecam_box, width, height)
    k = out_w / BASE_W
    if layout is Layout.FACECAM_STACK:
        return _facecam_stack(width, height, k, out_w, out_h, facecam_box)
    if layout is Layout.CENTER_SLICE:
        return _center_slice(width, height, k, out_w, out_h)
    if layout is Layout.FULL_WIDTH:
        return _full_width(width, height, k, out_w, out_h)
    return _square(width, height, k, out_w, out_h)
