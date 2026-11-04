"""prefetch.py: URL parsing, caption de-overlap, and the fetch-order policy.

The fetch-order tests mock the network functions and assert WHICH ones get
called, because the point of the policy is to avoid the rate-limited path."""
from __future__ import annotations

import json
import threading

import pytest

# --------------------------------------------------------------------------- IDs


@pytest.mark.parametrize("token,expected", [
    ("dQw4w9WgXcQ", ("dQw4w9WgXcQ", None)),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("dQw4w9WgXcQ", None)),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s", ("dQw4w9WgXcQ", None)),
    ("https://youtu.be/dQw4w9WgXcQ", ("dQw4w9WgXcQ", None)),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", ("dQw4w9WgXcQ", None)),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", ("dQw4w9WgXcQ", None)),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", ("dQw4w9WgXcQ", None)),
    # a watch URL that also carries a playlist is still that one video
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", ("dQw4w9WgXcQ", None)),
    ("https://www.youtube.com/playlist?list=PL123", (None, "PLAYLIST")),
    ("not a video", (None, "INVALID_URL")),
])
def test_extract_id(prefetch, token, expected):
    assert prefetch.extract_id(token) == expected


# ------------------------------------------------------------------ de-overlap

SENTENCE = ("the quick brown fox jumps over the lazy dog and keeps running "
            "far away from here until night").split()


def rolling(words, window=6, step=3):
    """Auto-caption style: each cue repeats the tail of the previous one."""
    return [{"text": " ".join(words[i:i + window]), "start": i * 0.5, "duration": 1.0}
            for i in range(0, len(words), step)]


def text_of(segs):
    return " ".join(s["text"] for s in segs)


def test_rolling_captions_collapse_to_the_original_sentence(prefetch):
    out = prefetch.deoverlap(rolling(SENTENCE))
    assert text_of(out) == " ".join(SENTENCE)


def test_clean_manual_subtitles_lose_no_words(prefetch):
    segs = [{"text": "alpha beta gamma", "start": 0, "duration": 2},
            {"text": "delta epsilon zeta", "start": 2, "duration": 2},
            {"text": "eta theta iota", "start": 4, "duration": 2}]
    assert text_of(prefetch.deoverlap(segs)) == text_of(segs)


def test_does_not_drop_a_word_that_merely_matches_the_end_of_the_previous_word(prefetch):
    segs = [{"text": "the cat sat", "start": 0, "duration": 1},
            {"text": "at noon", "start": 1, "duration": 1}]
    assert text_of(prefetch.deoverlap(segs)) == "the cat sat at noon"


def test_does_not_cut_a_longer_word_that_starts_like_the_previous_tail(prefetch):
    segs = [{"text": "I really know", "start": 0, "duration": 1},
            {"text": "knowledge is power", "start": 1, "duration": 1}]
    assert text_of(prefetch.deoverlap(segs)) == "I really know knowledge is power"


def test_a_single_repeated_word_across_cues_is_kept(prefetch):
    # "very very good": dropping one would silently change what was said
    segs = [{"text": "it is very", "start": 0, "duration": 1},
            {"text": "very good", "start": 1, "duration": 1}]
    assert text_of(prefetch.deoverlap(segs)) == "it is very very good"


def test_buckets_cues_into_quotable_chunks_with_monotonic_starts(prefetch):
    out = prefetch.deoverlap(rolling(SENTENCE * 6), bucket_seconds=14.0)
    starts = [s["start"] for s in out]
    assert starts == sorted(starts) and len(out) < len(rolling(SENTENCE * 6))


def test_empty_and_blank_input(prefetch):
    assert prefetch.deoverlap([]) == []
    assert prefetch.deoverlap([{"text": "  ", "start": 0, "duration": 1}]) == []


def _write_cache(tmp_path, vid, segs, **extra):
    (tmp_path / f"{vid}.json").write_text(
        json.dumps({"video_id": vid, "title": "t", "duration_seconds": 100,
                    "transcript": segs, **extra}), encoding="utf-8")


def test_ensure_deoverlapped_shrinks_once_and_is_idempotent(prefetch, tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    raw = rolling(SENTENCE * 4)
    _write_cache(tmp_path, "v" * 11, raw)
    first = prefetch.ensure_deoverlapped("v" * 11)
    d1 = json.loads((tmp_path / f"{'v' * 11}.json").read_text(encoding="utf-8"))
    assert d1["_deoverlapped"] is True and d1["_raw_segments"] == len(raw)
    second = prefetch.ensure_deoverlapped("v" * 11)
    d2 = json.loads((tmp_path / f"{'v' * 11}.json").read_text(encoding="utf-8"))
    assert first == second and d1["transcript"] == d2["transcript"]


def test_ensure_deoverlapped_keeps_raw_when_cleaning_would_destroy_the_text(
        prefetch, tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    raw = [{"text": "hello world", "start": i, "duration": 1} for i in range(40)]
    _write_cache(tmp_path, "w" * 11, raw)
    prefetch.ensure_deoverlapped("w" * 11)
    d = json.loads((tmp_path / f"{'w' * 11}.json").read_text(encoding="utf-8"))
    assert d["transcript"] == raw and d["_deoverlap_skipped"] is True


# ------------------------------------------------------------- fetch-order policy

VID = "abcdefghijk"
GOOD_META = {"title": "T", "channel": "C", "duration_seconds": 600, "upload_date": "2026-01-01",
             "view_count": 1, "language": "en-US", "has_en": True, "is_live": False}


class Harness:
    """Replaces the network functions and records the order they are called in."""

    def __init__(self, prefetch, monkeypatch, tmp_path, meta, yta_ok=True,
                 fetchpy=(0, "", "")):
        self.calls, self.logs, self.p, self.tmp = [], [], prefetch, tmp_path
        self.meta, self.yta_ok, self.fetchpy = meta, yta_ok, fetchpy
        monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(prefetch.time, "sleep", lambda s: None)
        monkeypatch.setattr(prefetch, "yt_metadata", self._meta)
        monkeypatch.setattr(prefetch, "yta_fetch", self._yta)
        monkeypatch.setattr(prefetch, "run_fetchpy", self._fetchpy)

    def _write(self):
        segs = [{"text": f"word{i} other{i}", "start": float(i), "duration": 1.0}
                for i in range(6)]
        _write_cache(self.tmp, VID, segs)

    def _meta(self, vid):
        self.calls.append("meta")
        return self.meta

    def _yta(self, vid, meta):
        self.calls.append("yta")
        if self.yta_ok:
            self._write()
            return True, ""
        return False, "RequestBlocked: 429"

    def _fetchpy(self, claims_dir, vid):
        self.calls.append("fetchpy")
        rc, out, err = self.fetchpy
        if rc == 0:
            self._write()
        return rc, out, err

    def run(self, min_seconds=16):
        return self.p.process(VID, None, min_seconds, threading.Semaphore(1),
                              lambda video=None, **kv: self.logs.append(kv))


def test_cache_hit_makes_no_network_calls(prefetch, monkeypatch, tmp_path):
    h = Harness(prefetch, monkeypatch, tmp_path, GOOD_META)
    h._write()
    r = h.run()
    assert r["status"] == "ok" and r["method"] == "cache_hit" and h.calls == []


def test_subtitle_api_is_tried_first_and_fetchpy_is_never_called(prefetch, monkeypatch, tmp_path):
    h = Harness(prefetch, monkeypatch, tmp_path, GOOD_META)
    r = h.run()
    assert r["status"] == "ok" and r["method"] == "yta"
    assert h.calls == ["meta", "yta"]


def test_falls_back_to_fetchpy_when_the_subtitle_api_fails(prefetch, monkeypatch, tmp_path):
    h = Harness(prefetch, monkeypatch, tmp_path, GOOD_META, yta_ok=False)
    r = h.run()
    assert r["status"] == "ok" and r["method"] == "fetchpy"
    assert h.calls == ["meta", "yta", "fetchpy"]


def test_fetchpy_429_is_retried_twice_then_reported(prefetch, monkeypatch, tmp_path):
    err = '429 Too Many Requests\n{"error": "NO_TRANSCRIPT", "message": "x"}'
    h = Harness(prefetch, monkeypatch, tmp_path, GOOD_META, yta_ok=False, fetchpy=(2, "", err))
    r = h.run()
    assert r["status"] == "NO_TRANSCRIPT" and r["method"] == "none"
    assert h.calls.count("fetchpy") == 3                 # first try + 2 retries
    assert h.logs[-1]["http_429"] == 2 and h.logs[-1]["retries"] == 2


def test_metadata_failure_skips_the_subtitle_api_and_uses_fetchpy(prefetch, monkeypatch, tmp_path):
    h = Harness(prefetch, monkeypatch, tmp_path, {"_error": "boom"})
    r = h.run()
    assert r["method"] == "fetchpy" and h.calls == ["meta", "fetchpy"]


@pytest.mark.parametrize("meta_patch,min_seconds,status", [
    ({"language": "es"}, 16, "NON_ENGLISH"),
    ({"is_live": True}, 16, "LIVE_STREAM"),
    ({"duration_seconds": 10}, 16, "TOO_SHORT"),
])
def test_rejections_happen_before_any_transcript_request(
        prefetch, monkeypatch, tmp_path, meta_patch, min_seconds, status):
    h = Harness(prefetch, monkeypatch, tmp_path, {**GOOD_META, **meta_patch})
    r = h.run(min_seconds)
    assert r["status"] == status and h.calls == ["meta"]


def test_invalid_input_is_rejected_without_touching_the_network(prefetch, monkeypatch, tmp_path):
    h = Harness(prefetch, monkeypatch, tmp_path, GOOD_META)
    out = prefetch.process("not a video", None, 16, threading.Semaphore(1),
                           lambda video=None, **kv: None)
    assert out["status"] == "INVALID_URL" and h.calls == []
