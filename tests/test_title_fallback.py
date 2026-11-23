"""When no moment passes the title checks, a chapter title about a real subject
is used instead of "<meeting> — topic 1" or "<meeting> — <day>"."""

from publish import describe


def test_a_subject_chapter_is_used_and_generic_ones_are_passed_over():
    titles = ["Intro", "Team updates", "Welcome and agenda", "Remote Raspberry Pi access with Tailscale"]
    assert describe._chapter_fallback(titles, set(), 60) == "Remote Raspberry Pi access with Tailscale"


def test_a_chapter_naming_a_speaker_or_too_short_is_skipped():
    titles = ["Ahsan explains the cache", "Caching", "Why the session cache misses"]
    assert describe._chapter_fallback(titles, {"ahsan"}, 60) == "Why the session cache misses"


def test_a_never_post_word_is_skipped():
    titles = ["Operations budget review for Q4", "Splitting IoT work across two boards"]
    assert describe._chapter_fallback(titles, set(), 60) == "Splitting IoT work across two boards"


def test_nothing_usable_gives_nothing():
    assert describe._chapter_fallback(["Intro", "Wrap-up", "Q&A"], set(), 60) == ""
    assert describe._chapter_fallback(["A long real subject title here"], set(), 10) == ""
