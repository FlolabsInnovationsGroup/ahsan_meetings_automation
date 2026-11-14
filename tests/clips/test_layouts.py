import re

import pytest

from clips import layouts
from clips.models import Layout, Moment, Platform, Source

BOX = (0.7, 0.05, 0.25, 0.25)


def _src(box=None):
    return Source(id="s", name="S", platform=Platform.FOLDER, facecam_box=box)


def _mom(layout=Layout.AUTO):
    return Moment(start_s=0, end_s=20, layout=layout)


def test_auto_without_facecam_is_square():
    assert layouts.choose_layout(_mom(), _src(), 1920, 1080) is Layout.SQUARE


def test_auto_with_facecam_is_stack():
    assert layouts.choose_layout(_mom(), _src(BOX), 1920, 1080) is Layout.FACECAM_STACK


@pytest.mark.parametrize("layout", list(Layout))
def test_never_returns_auto(layout):
    for box in (None, BOX):
        assert layouts.choose_layout(_mom(layout), _src(box), 1920, 1080) is not Layout.AUTO


def test_stack_without_box_falls_back_to_square():
    assert layouts.choose_layout(_mom(Layout.FACECAM_STACK), _src(), 1920, 1080) is Layout.SQUARE


def test_slice_needs_a_tall_source():
    assert layouts.choose_layout(_mom(Layout.CENTER_SLICE), _src(), 1920, 1080) is Layout.SQUARE
    assert layouts.choose_layout(_mom(Layout.CENTER_SLICE), _src(), 2560, 1440) is Layout.CENTER_SLICE


def test_explicit_layout_is_kept():
    assert layouts.choose_layout(_mom(Layout.FULL_WIDTH), _src(BOX), 1920, 1080) is Layout.FULL_WIDTH


@pytest.mark.parametrize("layout", list(Layout))
def test_every_filter_reads_input_and_writes_v(layout):
    g = layouts.video_filter(layout, 2560, 1440, facecam_box=BOX)
    assert g.startswith("[0:v]")
    assert g.endswith("[v]")
    assert "format=yuv420p" in g
    assert g.count("[") == g.count("]")


def test_square_crops_centre_square_and_places_below_title():
    g = layouts.video_filter(Layout.SQUARE, 1920, 1080)
    assert "crop=1080:1080:(iw-1080)/2:(ih-1080)/2" in g
    assert "scale=1080:1080" in g
    assert "overlay=0:520" in g
    assert "boxblur" in g


def test_full_width_scales_to_width_over_blur():
    g = layouts.video_filter(Layout.FULL_WIDTH, 1920, 1080)
    assert "scale=1080:-2[g]" in g
    assert "overlay=0:540" in g and "boxblur" in g


def test_center_slice_is_9_16():
    g = layouts.video_filter(Layout.CENTER_SLICE, 2560, 1440)
    assert "crop=810:1440" in g
    assert "scale=1080:1920" in g


def test_slice_on_small_source_uses_square():
    assert "overlay" in layouts.video_filter(Layout.CENTER_SLICE, 1920, 1080)


def test_stack_geometry_and_vstack():
    g = layouts.video_filter(Layout.FACECAM_STACK, 1920, 1080, facecam_box=BOX)
    assert "vstack=inputs=2" in g
    assert "crop=480:270:1344:54" in g  # the face box in pixels
    assert "scale=1080:640" in g and "scale=1080:1280" in g


def test_stack_gameplay_crop_avoids_the_face_box():
    g = layouts.video_filter(Layout.FACECAM_STACK, 1920, 1080, facecam_box=BOX)
    m = re.search(r"\[gm\]crop=(\d+):(\d+):(\d+):0", g)
    gw, _, gx = map(int, m.groups())
    face_x = 1344
    assert gx + gw <= face_x or gx >= face_x + 480


def test_stack_without_box_is_square():
    assert layouts.video_filter(Layout.FACECAM_STACK, 1920, 1080) == layouts.video_filter(Layout.SQUARE, 1920, 1080)


def test_crop_sizes_are_even():
    for w, h in [(1921, 1081), (1280, 720), (854, 480)]:
        g = layouts.video_filter(Layout.SQUARE, w, h)
        for pair in re.findall(r"crop=(\d+):(\d+)", g):
            assert all(int(x) % 2 == 0 for x in pair)


def test_other_output_size_scales_geometry():
    g = layouts.video_filter(Layout.SQUARE, 1920, 1080, out_w=540, out_h=960)
    assert "scale=540:540" in g and "overlay=0:260" in g
