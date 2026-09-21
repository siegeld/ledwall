"""`shuffle:` — play a library end to end, in a random order, forever."""

import random

import pytest

import time as _time

from marquee_core.render import ShufflePlayer, VideoDecoder


def _wait(cond, timeout=3.0):
    """Pre-warming happens on a worker thread; give it a moment."""
    end = _time.time() + timeout
    while _time.time() < end and not cond():
        _time.sleep(0.01)
    return cond()
from marquee_core.sources import Resolver, SourceError


# ---- the pool ------------------------------------------------------------

def _lib(tmp_path):
    root = tmp_path / "lib"
    (root / "2001").mkdir(parents=True)
    (root / "DfsrPrivate").mkdir()
    (root / "__DFSR_DIAGNOSTICS_TEST_FOLDER__").mkdir()
    (root / "2001" / "a.mp4").write_bytes(b"x")
    (root / "2001" / "b.MOV").write_bytes(b"x")
    (root / "notes.txt").write_text("not a film")
    (root / "._sidecar.mp4").write_bytes(b"x")
    (root / "DfsrPrivate" / "junk.mp4").write_bytes(b"x")
    (root / "__DFSR_DIAGNOSTICS_TEST_FOLDER__" / "junk.mp4").write_bytes(b"x")
    return Resolver(libraries={"films": str(root)})


def test_pool_finds_videos_and_skips_replication_junk(tmp_path):
    names = [p.rsplit("/", 1)[-1] for p in _lib(tmp_path).pool("films")]
    assert names == ["a.mp4", "b.MOV"]   # sorted, and nothing else


def test_pool_is_sorted_so_a_run_is_reproducible(tmp_path):
    p = _lib(tmp_path).pool("films")
    assert p == sorted(p)


def test_pool_can_scope_to_a_subfolder(tmp_path):
    assert len(_lib(tmp_path).pool("films/2001")) == 2


def test_pool_refuses_to_escape_the_library(tmp_path):
    with pytest.raises(SourceError):
        _lib(tmp_path).pool("films/../../etc")


def test_unknown_library_is_an_error(tmp_path):
    with pytest.raises(SourceError):
        _lib(tmp_path).pool("nope")


def test_empty_library_is_an_error(tmp_path):
    (tmp_path / "bare").mkdir()
    with pytest.raises(SourceError):
        Resolver(libraries={"bare": str(tmp_path / "bare")}).pool("bare")


# ---- the bag -------------------------------------------------------------

class _FakeDec:
    """Ends after n frames, like a real file running out."""
    made: list = []

    def __init__(self, path, w, h, fps, loop=False, start=0.0, fit="cover"):
        self.path, self.ended, self.left, self.fit = path, False, 2, fit
        _FakeDec.made.append(path)

    def next_frame(self):
        if self.left <= 0:
            self.ended = True
        self.left -= 1
        return object()

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    _FakeDec.made = []
    monkeypatch.setattr("marquee_core.render.VideoDecoder", _FakeDec)
    return _FakeDec


def test_plays_every_film_before_repeating_any(fake):
    pool = [f"f{i}.mp4" for i in range(8)]
    p = ShufflePlayer(pool, 8, 8, 1, rng=random.Random(7))
    for _ in range(8 * 4):
        p.next_frame()
    first8 = fake.made[:8]
    assert sorted(first8) == sorted(pool), "a bag must exhaust before repeating"


def test_reshuffle_does_not_replay_the_same_film_back_to_back(fake):
    pool = ["a.mp4", "b.mp4", "c.mp4"]
    for seed in range(25):
        _FakeDec.made = []
        p = ShufflePlayer(pool, 8, 8, 1, rng=random.Random(seed))
        for _ in range(3 * 6):
            p.next_frame()
        played = fake.made
        assert all(x != y for x, y in zip(played, played[1:])), \
            f"seed {seed} played the same film twice in a row across a reshuffle"


def test_a_file_that_yields_nothing_does_not_wedge_the_render_loop(monkeypatch):
    """An unreadable film must be skipped, not spun on forever."""
    class Dead:
        def __init__(self, path, w, h, fps, loop=False, start=0.0, fit="cover"):
            self.ended = False
        def next_frame(self):
            self.ended = True      # ends immediately, every time
            return object()
        def close(self):
            pass
    monkeypatch.setattr("marquee_core.render.VideoDecoder", Dead)
    p = ShufflePlayer([f"f{i}.mp4" for i in range(50)], 8, 8, 1,
                      rng=random.Random(1))
    assert p.next_frame() is not None    # returns, rather than looping forever


def test_single_film_library_still_works(fake):
    p = ShufflePlayer(["only.mp4"], 8, 8, 1, rng=random.Random(3))
    for _ in range(6):
        p.next_frame()
    assert set(fake.made) == {"only.mp4"}


def test_video_decoder_reports_eof_when_not_looping():
    d = VideoDecoder.__new__(VideoDecoder)
    d.proc = None
    d.ended = False
    d._last = object()
    d.next_frame()
    assert d.ended, "a finished file must be distinguishable from a stalled one"


# ---- random: independent picks -------------------------------------------

def test_random_picks_independently_so_repeats_are_possible(fake):
    """`random:` is not a bag -- a film may come round before the others."""
    pool = [f"f{i}.mp4" for i in range(4)]
    p = ShufflePlayer(pool, 8, 8, 1, rng=random.Random(11), pick="random")
    for _ in range(40 * 3):
        p.next_frame()
    played = fake.made
    # In a bag the first len(pool) plays are always distinct. Independent
    # picking must violate that somewhere over many draws, or it is a bag.
    windows = [played[i:i + len(pool)] for i in range(0, len(played) - len(pool))]
    assert any(len(set(w)) < len(pool) for w in windows), \
        "every window was a permutation -- this is drawing without replacement"


def test_random_never_repeats_back_to_back(fake):
    pool = [f"f{i}.mp4" for i in range(3)]
    for seed in range(30):
        _FakeDec.made = []
        p = ShufflePlayer(pool, 8, 8, 1, rng=random.Random(seed), pick="random")
        for _ in range(3 * 10):
            p.next_frame()
        played = fake.made
        assert all(x != y for x, y in zip(played, played[1:])), \
            f"seed {seed} played the same film twice in a row"


def test_random_with_one_film_does_not_deadlock(fake):
    p = ShufflePlayer(["only.mp4"], 8, 8, 1, rng=random.Random(2), pick="random")
    for _ in range(6):
        p.next_frame()
    assert set(fake.made) == {"only.mp4"}


def test_the_layers_fit_reaches_the_decoder(fake):
    """A shuffling layer must pass its `fit` down, or every film is cropped."""
    p = ShufflePlayer(["a.mp4"], 8, 8, 1, rng=random.Random(1), fit="smart")
    p.next_frame()
    assert p.dec.fit == "smart"


# ---- pre-warming: the next film is open BEFORE the current one ends -------
#
# Opening a multi-gigabyte file costs 120-480ms and the render loop is single
# threaded, so paying it at the transition stalled the whole wall for 3 to 9
# frames every time a film changed.

def test_the_next_film_is_opened_while_this_one_plays(fake):
    p = ShufflePlayer([f"f{i}.mp4" for i in range(5)], 8, 8, 1,
                      rng=random.Random(4))
    p.next_frame()
    _wait(lambda: p.nxt is not None)
    assert p.nxt is not None, "nothing pre-warmed -- the transition will stall"
    assert p.nxt_path != p.current, "pre-warmed the film already playing"


def test_a_transition_uses_the_prewarmed_decoder(fake):
    p = ShufflePlayer([f"f{i}.mp4" for i in range(5)], 8, 8, 1,
                      rng=random.Random(6))
    _wait(lambda: p.nxt is not None)
    warmed, path = p.nxt, p.nxt_path
    p._advance()
    assert p.dec is warmed, "spawned a new decoder instead of using the warm one"
    assert p.current == path


def test_next_frame_holds_rather_than_blocking_when_nothing_is_warm(fake):
    """A held frame is a held frame; a blocked loop stalls the whole wall."""
    p = ShufflePlayer(["a.mp4", "b.mp4"], 8, 8, 1, rng=random.Random(8))
    _wait(lambda: p.nxt is not None)
    p.nxt = None                      # pretend the worker has not finished
    p._warming = True                 # ...and is still going
    before = p.plays
    for _ in range(6):
        assert p.next_frame() is not None
    assert p.plays == before, "advanced with nothing warm -- that blocks"


def test_close_releases_the_prewarmed_decoder_too(fake):
    """It is a live ffmpeg; leaking it leaks a process per show change."""
    closed = []
    class Tracking(_FakeDec):
        def close(self):
            closed.append(self.path)
    p = ShufflePlayer(["a.mp4", "b.mp4"], 8, 8, 1, rng=random.Random(5))
    _wait(lambda: p.nxt is not None)
    p.dec.__class__ = Tracking
    p.nxt.__class__ = Tracking
    warm = p.nxt.path
    p.close()
    assert warm in closed, "the pre-warmed decoder was leaked"


def test_source_size_is_probed_once_per_file():
    """ffprobe costs 110-480ms; smart fit needs it for every decoder."""
    from marquee_core import render
    render._SRC_SIZE_CACHE.clear()
    calls = []
    d = VideoDecoder.__new__(VideoDecoder)
    d.path, d.w, d.h, d.fit = "same.mp4", 8, 8, "smart"
    render._SRC_SIZE_CACHE["same.mp4"] = (1920, 1080)
    assert d._source_size() == (1920, 1080)
    # a second decoder for the same file must reuse it
    e = VideoDecoder.__new__(VideoDecoder)
    e.path, e.w, e.h, e.fit = "same.mp4", 8, 8, "smart"
    assert e._source_size() == (1920, 1080)
