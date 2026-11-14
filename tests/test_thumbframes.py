"""Thumbnail frame picker: scoring, classifying, picking and the safe crop.
Synthetic numbers only."""

import shutil
import time

import numpy as np
import pytest

from core.config import get_settings
from publish import thumbframes as tf

W, H = 1920, 1080


def mk(t, yavg=100.0, ylow=45.0, entropy=4.0, blur=4.2, edge=10.0, small=None):
    if small is None:
        small = np.zeros((tf.SMALL_H, tf.SMALL_W), np.uint8)
    return tf.Row(t, yavg, ylow, entropy, blur, edge, small)


SHARE = {"ylow": 45.0, "blur": 4.2}
CAMERA = {"ylow": 1.0, "blur": 6.4}


def test_parse_recorded_metadata_into_rows():
    def block(i, t, yavg, ylow, ent, blur):
        return (
            f"frame:{i}    pts:{i * 1000}    pts_time:{t}\n"
            f"lavfi.signalstats.YLOW={ylow}\nlavfi.signalstats.YAVG={yavg}\n"
            f"lavfi.entropy.entropy.normal.Y={ent}\nlavfi.blur={blur}\n"
        )

    m = block(0, 0, 120.5, 41, 4.1, 4.0) + block(1, 5.6, 60.0, 1, 3.0, 6.2)
    e = "".join(
        f"frame:{i}    pts:0    pts_time:0\nlavfi.signalstats.YAVG={v}\n" for i, v in enumerate((12.5, 3.25))
    )
    raw = bytes(range(256)) * 17  # at least two 64x34 frames
    rows = tf.parse_rows(m, e, raw[: 2 * tf.SMALL_W * tf.SMALL_H])
    assert [r.t for r in rows] == [0.0, 5.6]
    assert rows[0].ylow == 41 and rows[0].yavg == 120.5 and rows[0].blur == 4.0
    assert rows[1].edge == 3.25 and rows[1].entropy == 3.0
    assert rows[0].small.shape == (tf.SMALL_H, tf.SMALL_W)


def test_classify_share_gallery_and_blank():
    assert tf.classify(mk(0, **SHARE)) == "share"
    assert tf.classify(mk(0, **CAMERA)) == "camera"
    assert tf.classify(mk(0, yavg=5.0)) == "blank"
    assert tf.classify(mk(0, entropy=0.1)) == "blank"


def test_pick_prefers_a_share_inside_the_moment():
    rows = [
        mk(90, **SHARE, edge=11),  # a slightly better-looking share, but outside the moment
        mk(110, **SHARE, edge=9),
        mk(120, **CAMERA),
    ]
    p = tf.pick(rows, 100, 130, src_w=W, src_h=H)
    assert p.kind == "share" and p.t == 110
    assert p.crop is not None


def test_pick_is_a_card_when_only_camera_frames_exist_and_faces_are_off():
    rows = [mk(100, **CAMERA), mk(110, **CAMERA), mk(120, yavg=3.0)]
    p = tf.pick(rows, 100, 130, src_w=W, src_h=H)
    assert (p.kind, p.t, p.crop) == ("card", None, None)
    assert tf.pick(rows, 100, 130, faces=True, src_w=W, src_h=H).kind == "camera"
    assert tf.pick(rows, 500, 530, src_w=W, src_h=H).kind == "card"  # nothing near


def test_pick_ties_go_to_the_frame_nearest_the_peak():
    rows = [mk(100, **SHARE), mk(110, **SHARE), mk(120, **SHARE)]
    assert tf.pick(rows, 100, 130, peak=121, src_w=W, src_h=H).t == 120
    assert tf.pick(rows, 100, 130, peak=99, src_w=W, src_h=H).t == 100
    with pytest.raises(TypeError):  # the real frame size must always be passed
        tf.pick(rows, 100, 130)


def _stack_small(body_value, n=4):
    """A static top bar (rows 0-5) with a changing body below."""
    out = []
    for i in range(n):
        s = np.full((tf.SMALL_H, tf.SMALL_W), 80, np.uint8)
        s[6:, :] = 0 if i % 2 == 0 else body_value
        out.append(mk(i * 10, **SHARE, small=s))
    return out


def test_content_box_is_the_changing_body_without_the_bottom_strip():
    rows = _stack_small(255)
    x, y, w, h = tf.content_box(rows, W, H, focus=rows[1])
    sy = H / tf.SMALL_H
    assert y >= 6 * sy - 2  # the static top bar is not in the crop
    assert y + h <= H * (1 - tf.BOTTOM_DROP) + 1  # no timestamp strip
    assert abs(w / h - 16 / 9) < 0.02
    assert x % 2 == 0 and y % 2 == 0 and w % 2 == 0 and h % 2 == 0


def test_short_changing_box_below_a_static_bar_never_reaches_the_bar():
    rows = []
    for i in range(4):
        s = np.full((tf.SMALL_H, tf.SMALL_W), 80, np.uint8)
        s[4:13, :] = 0 if i % 2 == 0 else 255
        rows.append(mk(i, **SHARE, small=s))
    for sw, sh in ((1920, 1080), (1920, 1020)):
        _x, y, _w, h = tf.content_box(rows, sw, sh, focus=rows[1])
        assert y >= 4 * sh / tf.SMALL_H - 2  # not above the changing box
        assert y + h <= sh * (1 - tf.BOTTOM_DROP) + 1


def test_content_box_window_stays_inside_the_frame():
    # Change only in the far right-hand corner: the window must still fit.
    rows = []
    for i in range(4):
        s = np.full((tf.SMALL_H, tf.SMALL_W), 50, np.uint8)
        s[8:30, 54:] = 0 if i % 2 == 0 else 255
        rows.append(mk(i, **SHARE, small=s))
    x, y, w, h = tf.content_box(rows, W, H, focus=rows[1])
    assert x >= 0 and y >= 0 and x + w <= W and y + h <= H * (1 - tf.BOTTOM_DROP) + 1
    assert w >= 2 and h >= H * tf.MIN_WINDOW_HEIGHT - 2


def test_short_run_uses_the_fixed_safe_crop():
    x, y, w, h = tf.content_box([mk(0, **SHARE), mk(5, **SHARE)], W, H)
    assert y >= H * tf.SAFE_TOP - 2
    assert x >= 0 and x + w <= W and y + h <= H * (1 - tf.BOTTOM_DROP) + 1
    assert abs(w / h - 16 / 9) < 0.02


def test_thumbs_row_schema():
    r = tf.row("short", 2, 10, 55.5, 30, "share", (0, 2, 1280, 720), "thumbnails/base_s2.jpg", {"score": 1.5})
    assert set(r) == {"video", "index", "source_start", "source_end", "t", "kind", "crop", "file", "scores"}
    assert r["crop"] == [0, 2, 1280, 720]
    assert tf.row("full", 0, 0, 1, None, "card")["file"] is None
    with pytest.raises(ValueError):
        tf.row("clip", 0, 0, 1, None, "card")
    with pytest.raises(ValueError):
        tf.row("full", 0, 0, 1, None, "face")


FFMPEG = shutil.which(get_settings().ffmpeg_bin) or shutil.which("ffmpeg")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_real_clip_scores_and_extracts(tmp_path):
    from core.proc import run_checked

    clip = tmp_path / "clip.mp4"
    run_checked(
        [
            get_settings().ffmpeg_bin, "-hide_banner", "-y", "-f", "lavfi",
            "-i", "testsrc2=s=1280x720:r=10:d=20,drawbox=x=0:y=0:w=1280:h=60:color=gray:t=fill",
            "-g", "30", "-pix_fmt", "yuv420p", str(clip),
        ],
        timeout=60,
    )
    t0 = time.monotonic()
    rows = tf.score_keyframes(clip)
    assert len(rows) >= 5
    assert all(r.small.shape == (tf.SMALL_H, tf.SMALL_W) for r in rows)
    out = tmp_path / "thumbnails" / "base.jpg"
    tf.extract(clip, rows[2].t, (0, 60, 1136, 639 // 2 * 2), out)
    assert time.monotonic() - t0 < 10
    assert 0 < out.stat().st_size < 2_000_000
    head = out.read_bytes()[:2]
    assert head == b"\xff\xd8"
    probe = run_checked(
        [get_settings().ffmpeg_bin, "-hide_banner", "-i", str(out), "-f", "null", "-"], timeout=30
    )
    assert "1280x720" in probe.stderr
