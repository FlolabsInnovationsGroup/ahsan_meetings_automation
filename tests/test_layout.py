"""Per-clip picture analysis (slice/layout.py) on synthetic frames only."""

import os
import shutil
import subprocess

import numpy as np
import pytest

from slice import ffmpeg_wrapper, layout

W, H = 480, 270  # sampled frame size
SW, SH = 1920, 1080  # pretend source size (scale 4)
N = 24


def rng():
    return np.random.default_rng(7)


def blank(level=40, n=N):
    return np.full((n, H, W), level, np.uint8)


def slide_clip(x0=100, x1=400, y0=30, y1=240, n=N):
    """A white slide on a darker window; one page change at steps 8 and 12."""
    f = blank(40, n)
    f[:, y0:y1, x0:x1] = 235
    f[:, y0 + 20 : y0 + 30, x0 + 20 : x1 - 20] = 90  # a static title bar
    f[8:12, 120:160, 180:240] = 150  # content changes (two steps: in, out)
    return f


def gallery_clip(bars=30, n=N):
    """3x2 noisy tiles with letterbox bars top and bottom."""
    r = rng()
    f = np.zeros((n, H, W), np.uint8)
    levels = [[60, 140, 220], [220, 60, 140]]
    top, bottom = bars, H - bars
    th = (bottom - top) // 2
    for row in range(2):
        for col in range(3):
            base = levels[row][col]
            ys, xs = top + row * th, col * (W // 3)
            noise = r.normal(0, 10, (n, th, W // 3))
            f[:, ys : ys + th, xs : xs + W // 3] = np.clip(base + noise, 0, 255).astype(np.uint8)
    return f


def speaker_clip(x0=300, x1=380, y0=60, y1=200, n=N):
    r = rng()
    f = blank(50, n)
    f[:, y0:y1, x0:x1] = r.integers(100, 200, (n, y1 - y0, x1 - x0), dtype=np.uint8)
    return f


def even_inside(panel):
    x, y, w, h = panel
    assert all(v % 2 == 0 for v in panel)
    assert x >= 0 and y >= 0 and x + w <= SW and y + h <= SH


def test_static_slide_is_screen_and_panel_is_the_slide():
    lay = layout.analyse(slide_clip(), SW, SH, "shared_screen_with_gallery_view")
    assert lay.kind == "screen"
    even_inside(lay.panel)
    x, y, w, h = lay.panel
    for got, want in zip((x, y, x + w, y + h), (400, 120, 1600, 960), strict=True):
        assert abs(got - want) <= 12
    assert lay.change_box is not None
    assert lay.focus_x is None
    assert lay.metrics["changed_px_share"] <= layout.SCREEN_MAX_CHANGED_SHARE


def slide_with_image(change=None, image=(110, 300)):
    """White slide at x 100-400 with a dark image block inside (long vertical edge)."""
    f = blank(40)
    f[:, 30:240, 100:400] = 235
    f[:, 60:200, image[0] : image[1]] = 60
    if change is not None:
        f[8:12, change[0] : change[1], change[2] : change[3]] = 150
    return f


def test_static_slide_with_an_inner_image_edge_keeps_the_whole_slide():
    lay = layout.analyse(slide_with_image(), SW, SH, "shared_screen_with_gallery_view")
    assert lay.kind == "screen"
    x, _y, w, _h = lay.panel
    assert abs(x - 400) <= 12 and abs(x + w - 1600) <= 12


def test_change_on_one_side_does_not_pick_an_inner_edge_that_misses_it():
    # a bullet appears right of the image edge: the pair must enclose it
    lay = layout.analyse(slide_with_image(change=(80, 100, 320, 380)), SW, SH, "shared_screen_with_gallery_view")
    assert lay.kind == "screen"
    x, _y, w, _h = lay.panel
    assert x + w >= 1500


def viewer_clip():
    """A PDF viewer: dark window, a toolbar with a page counter on top, a thumbnail column on the left
    whose highlight moves with the page, and a light slide (x 120-450, y 60-245) that changes."""
    f = blank(40)
    f[:, 10:45, 20:460] = 70  # toolbar
    f[:, 60:245, 20:80] = 150  # thumbnail column
    f[:, 60:245, 120:450] = 235  # the slide
    f[:, 80:95, 140:430] = 90  # its static title bar
    f[8:12, 110:130, 200:400] = 150  # the page changes: the slide's content
    f[8:12, 70:80, 30:70] = 90  # the thumbnail highlight moves
    f[8:12, 20:25, 230:250] = 0  # the page counter ticks over
    return f


def test_viewer_chrome_beside_and_above_the_slide_is_left_out_of_the_crop():
    lay = layout.analyse(viewer_clip(), SW, SH, "shared_screen_with_gallery_view")
    assert lay.kind == "screen"
    even_inside(lay.panel)
    x, y, w, h = lay.panel
    # the slide is x 480-1800, y 240-980 in source pixels: not the thumbnails (x 80) and not the toolbar (y 40)
    assert x >= 480 - 16, lay.panel
    assert abs(x + w - 1800) <= 16, lay.panel
    assert y >= 240 - 16, lay.panel
    assert y + h <= 980 + 16, lay.panel
    assert lay.metrics["panel_from_edges"] is True


def test_the_main_block_of_change_ignores_small_changes_apart_from_it():
    counts = np.zeros((100, 200), np.int32)
    counts[30:90, 60:190] = 2  # the slide
    counts[40:60, 5:30] = 2  # a thumbnail highlight, a gap away
    counts[5:10, 100:120] = 2  # a page counter above
    assert layout._main_bbox(counts) == (60, 30, 190, 90)
    assert layout._main_bbox(np.zeros((10, 10), np.int32)) is None


def test_best_pair_enclose_drops_pairs_that_miss_the_change_and_ties_go_widest():
    act = np.zeros(480)
    edges = [100.0, 300.0, 400.0]
    assert layout._best_pair(edges, act, 480, 190, enclose=(320, 380)) == (100.0, 400.0)
    assert layout._best_pair(edges, act, 480, 190) == (100.0, 400.0)


def with_stamp(frames, y0=250, y1=254, x0=420, x1=472):
    """Zoom's timestamp: a few bright text rows in the bottom right of the black bar."""
    out = frames.copy()
    out[:, y0:y1, x0:x1:2] = 200
    return out


def test_zoom_timestamp_on_the_black_bar_is_found_and_boxed():
    frames = with_stamp(gallery_clip(bars=30))
    lay = layout.analyse(frames, SW, SH, "gallery_view")
    assert lay.kind == "gallery"
    assert lay.stamp is not None
    x, y, w, h = lay.stamp
    assert all(v % 2 == 0 for v in lay.stamp)
    assert x <= 420 * 4 and x + w >= 472 * 4 - 8 and y <= 250 * 4 and y + h >= 254 * 4
    assert w < 0.2 * SW and h < 0.06 * SH


def test_no_timestamp_means_no_box():
    assert layout.analyse(gallery_clip(), SW, SH, "gallery_view").stamp is None
    assert layout.analyse(slide_clip(), SW, SH, "shared_screen_with_gallery_view").stamp is None
    # bright picture right up to the corner: nothing to blur there
    assert layout._stamp_box(np.full((H, W), 200, np.uint8), SW, SH) is None


def test_noisy_tiles_with_bars_is_gallery_and_content_box_drops_the_bars():
    lay = layout.analyse(gallery_clip(), SW, SH, "gallery_view")
    assert lay.kind == "gallery"
    assert lay.metrics["content_box"] == [0, 30, W, H - 30]
    even_inside(lay.panel)
    assert lay.panel == (0, 120, SW, 840)


def test_off_centre_face_is_speaker_with_focus_near_it():
    lay = layout.analyse(speaker_clip(), SW, SH, "active_speaker")
    assert lay.kind == "speaker"
    assert lay.focus_x is not None
    assert abs(lay.focus_x - 340 * 4) <= 60


def test_ticking_clock_corner_stops_the_panel_above_it():
    f = slide_clip(x0=100, x1=460, y0=30, y1=262)
    r = rng()
    f[:, 257:262, 420:455] = r.choice(np.array([60, 230], np.uint8), size=(N, 5, 35))
    lay = layout.analyse(f, SW, SH, "shared_screen_with_gallery_view")
    assert lay.kind == "screen"
    _x, y, _w, h = lay.panel
    assert y + h <= 257 * 4
    assert lay.metrics["clock_top"] is not None


def test_tiny_panel_falls_back_to_fit():
    f = blank(40)
    f[:, 100:150, 100:200] = 235
    f[8:12, 110:130, 110:150] = 120
    lay = layout.analyse(f, SW, SH, "shared_screen")
    assert lay.kind == "fit"
    assert lay.panel == (0, 0, SW, SH)
    assert lay.notes


def test_half_screen_half_gallery_is_fit():
    n = 40
    f = np.concatenate([slide_clip(n=n // 2), gallery_clip(bars=0, n=n // 2)])
    lay = layout.analyse(f, SW, SH, "shared_screen_with_gallery_view")
    assert lay.kind == "fit"
    assert lay.panel == (0, 0, SW, SH)
    assert "switches" in lay.notes[0]


@pytest.mark.parametrize("frames", [None, np.zeros((0, H, W), np.uint8), blank(n=2), "nonsense"])
def test_too_little_input_is_fit_with_a_note(frames):
    lay = layout.analyse(frames, SW, SH, None)
    assert lay.kind == "fit"
    assert lay.panel == (0, 0, SW, SH)
    assert lay.notes


def test_mixed_recording_type_uses_the_edge_rule():
    assert layout.analyse(gallery_clip(), SW, SH, "gallery_view+shared_screen").kind == "gallery"
    assert layout.analyse(gallery_clip(), SW, SH, None).kind == "gallery"
    small = speaker_clip(x0=300, x1=380, y0=100, y1=180)  # one small face: no long edges
    lay = layout.analyse(small, SW, SH, "a+b")
    assert lay.kind == "speaker"
    assert lay.focus_x is not None


def test_layout_is_frozen_and_has_the_renderer_fields():
    lay = layout.analyse(slide_clip(), SW, SH, None)
    for name in ("kind", "panel", "change_box", "focus_x", "metrics", "notes"):
        assert hasattr(lay, name)
    with pytest.raises(AttributeError):
        lay.kind = "fit"


def _ffmpeg_ok():
    b = ffmpeg_wrapper.ffmpeg_bin()
    return bool(shutil.which(b) or os.path.exists(b))


@pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg is not available")
def test_sample_frames_shape_and_content_box_with_ffmpeg(tmp_path):
    src = tmp_path / "bars.mp4"
    subprocess.run(
        [
            ffmpeg_wrapper.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=640x270:r=25:d=4",
            "-vf", "pad=640:360:0:45:black", "-pix_fmt", "yuv420p", str(src),
        ],
        check=True,
        timeout=60,
    )
    frames = layout.sample_frames(src, 0, 4, src_size=(640, 360))
    assert frames.shape == (8, 270, 480)
    assert frames.dtype == np.uint8
    lay = layout.analyse(frames, 640, 360, None)
    x0, y0, x1, y1 = lay.metrics["content_box"]
    assert abs(y0 - 34) <= 4 and abs(y1 - 236) <= 4
    assert x0 <= 2 and x1 >= W - 2
    done = layout.analyse_clip(src, 0, 4, 640, 360, None)
    assert done.metrics["content_box"] == lay.metrics["content_box"]


def test_analyse_clip_never_raises_on_a_missing_file(tmp_path):
    lay = layout.analyse_clip(tmp_path / "nope.mp4", 0, 4, SW, SH, None)
    assert lay.kind == "fit"
