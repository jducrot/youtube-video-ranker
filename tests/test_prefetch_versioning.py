"""Cache versioning for the caption cleaner. The raw cues are kept in a sidecar so a fix
to the cleaning code can be re-applied without re-fetching, and a cache written by old
code is never 'cleaned' twice."""
from __future__ import annotations

import json

from test_prefetch import SENTENCE, _write_cache, rolling, text_of


def _read(tmp_path, vid):
    return json.loads((tmp_path / f"{vid}.json").read_text(encoding="utf-8"))


def test_first_pass_keeps_the_raw_cues_and_stamps_the_version(prefetch, tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    vid, raw = "a" * 11, rolling(SENTENCE * 4)
    _write_cache(tmp_path, vid, raw)
    prefetch.ensure_deoverlapped(vid)
    side = json.loads((tmp_path / f"{vid}.raw.json").read_text(encoding="utf-8"))
    assert _read(tmp_path, vid)["_deoverlap_version"] == prefetch.DEOVERLAP_VERSION
    assert side["transcript"] == raw


def test_a_stale_version_is_rederived_from_the_raw_cues(prefetch, tmp_path, monkeypatch):
    """The scenario of the earlier word-dropping bug: old code left a bad transcript.
    With the raw cues kept, the fixed cleaner repairs the cache without re-fetching."""
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    vid, raw = "b" * 11, rolling(SENTENCE * 4)
    _write_cache(tmp_path, vid, [{"text": "damaged by an old bug", "start": 0, "duration": 1}],
                 _deoverlapped=True, _deoverlap_version=1)
    (tmp_path / f"{vid}.raw.json").write_text(json.dumps({"video_id": vid, "transcript": raw}), encoding="utf-8")
    prefetch.ensure_deoverlapped(vid)
    d = _read(tmp_path, vid)
    assert d["_deoverlap_version"] == prefetch.DEOVERLAP_VERSION
    assert text_of(d["transcript"]) == " ".join(SENTENCE * 4)


def test_a_current_version_is_not_touched(prefetch, tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    vid = "e" * 11
    _write_cache(tmp_path, vid, rolling(SENTENCE * 4))
    prefetch.ensure_deoverlapped(vid)
    before = (tmp_path / f"{vid}.json").read_bytes()
    prefetch.ensure_deoverlapped(vid)
    assert (tmp_path / f"{vid}.json").read_bytes() == before


def test_a_legacy_cache_with_no_raw_cues_is_left_alone(prefetch, tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    vid = "c" * 11
    already_clean = [{"text": "already processed text", "start": 0, "duration": 1}]
    _write_cache(tmp_path, vid, already_clean, _deoverlapped=True)       # old code: a flag, no version
    prefetch.ensure_deoverlapped(vid)
    d = _read(tmp_path, vid)
    assert d["transcript"] == already_clean and "_deoverlap_version" not in d
    assert not (tmp_path / f"{vid}.raw.json").exists()     # never fabricate a "raw" from cleaned text


def test_the_safety_guard_also_stamps_the_version(prefetch, tmp_path, monkeypatch):
    monkeypatch.setattr(prefetch, "CACHE_DIR", tmp_path)
    vid = "d" * 11
    _write_cache(tmp_path, vid, [{"text": "hello world", "start": i, "duration": 1} for i in range(40)])
    prefetch.ensure_deoverlapped(vid)
    d = _read(tmp_path, vid)
    assert d["_deoverlap_skipped"] is True and d["_deoverlap_version"] == prefetch.DEOVERLAP_VERSION
