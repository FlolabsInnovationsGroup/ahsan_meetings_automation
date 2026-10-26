"""ffmpeg 7 reads a filter graph from a file with `-/filter_complex`; before 7
(Debian 12's ffmpeg 5.1 in the Docker image) only `-filter_complex_script`
exists, and ffmpeg 8+ no longer knows that one. The command builders must pick
the spelling of the ffmpeg they will actually run on."""

import pytest

from slice import ffmpeg_wrapper as fw


@pytest.fixture(autouse=True)
def _fresh_version_cache():
    """The version of a binary is cached for the whole process: never let what
    a test here told it leak into one that runs the real ffmpeg."""
    fw.ffmpeg_major.cache_clear()
    yield
    # the real function again, or still a test's stand-in (monkeypatch is
    # undone after this fixture): the stand-in has nothing cached
    getattr(fw.ffmpeg_major, "cache_clear", lambda: None)()


class _Done:
    def __init__(self, stdout):
        self.stdout = stdout


@pytest.mark.parametrize(
    ("first_line", "major"),
    [
        ("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright (c) 2000-2021 the FFmpeg developers", 4),
        ("ffmpeg version 5.1.9-0+deb12u1 Copyright (c) 2000-2025 the FFmpeg developers", 5),
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers", 6),
        ("ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2025 the FFmpeg developers", 7),
        ("ffmpeg version n7.1-3-g1ab2c3d Copyright (c) 2000-2024 the FFmpeg developers", 7),
        ("ffmpeg version 9.0.1-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers", 9),
        # a git build says nothing about its release: unknown, treated as recent
        ("ffmpeg version N-117234-gabcdef0123 Copyright (c) 2000-2024 the FFmpeg developers", None),
        ("", None),
        ("this is not ffmpeg", None),
    ],
)
def test_the_major_version_is_read_from_the_first_line(first_line, major):
    assert fw.parse_ffmpeg_major(first_line + "\nbuilt with gcc 12\n") == major


def test_the_version_is_asked_once_per_binary(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return _Done("ffmpeg version 5.1.9-0+deb12u1 Copyright")

    monkeypatch.setattr(fw, "run_checked", fake_run)
    assert [fw.ffmpeg_major("ffmpeg") for _ in range(3)] == [5, 5, 5]
    assert calls == [["ffmpeg", "-hide_banner", "-version"]]


def test_a_missing_or_broken_ffmpeg_is_unknown_not_an_error(monkeypatch):
    def boom(cmd, timeout=None):
        raise FileNotFoundError("no ffmpeg")

    monkeypatch.setattr(fw, "run_checked", boom)
    assert fw.ffmpeg_major("not-installed") is None


@pytest.mark.parametrize(
    ("major", "option"),
    [
        (4, "-filter_complex_script"),
        (5, "-filter_complex_script"),
        (6, "-filter_complex_script"),
        (7, "-/filter_complex"),
        (8, "-/filter_complex"),
        (9, "-/filter_complex"),
        (None, "-/filter_complex"),  # unknown: a recent git build, or ffmpeg isn't there to ask
    ],
)
def test_the_graph_file_is_read_with_the_spelling_of_that_ffmpeg(monkeypatch, tmp_path, major, option):
    monkeypatch.setattr(fw, "ffmpeg_major", lambda binary: major)
    assert fw.filter_graph_args(tmp_path / "graph.txt") == [option, str(tmp_path / "graph.txt")]


@pytest.mark.parametrize(("major", "option", "other"), [
    (5, "-filter_complex_script", "-/filter_complex"),
    (9, "-/filter_complex", "-filter_complex_script"),
])
def test_every_command_that_reads_a_graph_uses_it(monkeypatch, tmp_path, major, option, other):
    monkeypatch.setattr(fw, "ffmpeg_major", lambda binary: major)
    graph = tmp_path / "g.txt"
    piece = fw.AudioPiece(src_start=0, samples=48000, fade_in=False, fade_out=False)
    cmd = fw.build_audio_chunk_cmd(tmp_path / "a.flac", [piece], graph, tmp_path / "out.flac")
    assert cmd[cmd.index(option) + 1] == str(graph)
    assert other not in cmd
    # no path is left inline: the graph really is in the file
    assert graph.read_text(encoding="utf-8").strip()


def test_no_command_builder_spells_the_option_itself():
    import inspect
    source = inspect.getsource(fw)
    # the only place that knows either spelling is filter_graph_args
    assert source.count('"-/filter_complex"') == 1
    assert source.count('"-filter_complex_script"') == 1
