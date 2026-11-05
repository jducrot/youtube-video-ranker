#!/usr/bin/env python3
"""Phase A of youtube-video-ranker: fetch every video's transcript into the
shared youtube-claims cache, with BOUNDED concurrency.

Why this exists: fetching is the only stage that touches YouTube, and YouTube
rate-limits (HTTP 429) aggressively. Firing one subagent per video in parallel
429-storms at 10-15 videos. This script is the throttled front door: it fetches
at most --concurrency videos at once, retries 429s with jittered backoff, and
tries the English subtitle API first (yt-dlp's subtitle path is the one that gets HTTP 429) and falls back to youtube-claims fetch.py.
Once transcripts are cached, Phase B (the inventory subagents) reads the cache
only -- no network -- so it can fan out with no concurrency limit.

It never aborts the batch: each video gets a status, failures included, and the
JSON ledger it prints to stdout is what the orchestrator uses to decide which
videos proceed to inventory and which are listed as rejected.

Run WITHOUT ``-I`` (it needs user site-packages for yt_dlp / youtube_transcript_api).

Usage:
  python3 prefetch.py --claims-dir /path/to/youtube-claims --run-id R \
      [--concurrency 3] [--min-seconds 16] [--telemetry-dir DIR] \
      VIDEO_OR_URL [VIDEO_OR_URL ...]

Exit code is 0 unless no video could be fetched at all.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CACHE_DIR = Path.home() / "youtube-reports" / ".cache"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MIN_OVERLAP_WORDS = 2   # de-overlap only collapses repeats of at least this many words
# Bump this whenever deoverlap() changes behaviour. A cache whose _deoverlap_version
# differs is re-derived from its raw sidecar (<id>.raw.json) the next time prefetch
# sees it; the youtube-claims cache then misses on the new transcript hash and the
# inventory is redone, which is what you want after a cleaning bug fix.
DEOVERLAP_VERSION = 2
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


# ---------------------------------------------------------------------------
# Telemetry (best-effort; never fail the fetch because logging failed)
# ---------------------------------------------------------------------------
def make_logger(run_id, telemetry_dir):
    script = Path(__file__).with_name("telemetry.py")

    def log(video=None, **kv):
        args = [sys.executable, "-I", str(script), "--telemetry-dir",
                str(telemetry_dir), "log", "--run-id", run_id,
                "--stage", "fetch", "--event", "fetch"]
        if video:
            args += ["--video", video]
        if kv:
            args += ["--kv"] + [f"{k}={v}" for k, v in kv.items()]
        try:
            subprocess.run(args, env=ENV, capture_output=True, timeout=20)
        except Exception:
            pass

    return log


# ---------------------------------------------------------------------------
# URL / ID handling
# ---------------------------------------------------------------------------
def extract_id(token: str):
    """Return (video_id, reason). reason is set only when it is NOT a video."""
    token = token.strip()
    if ID_RE.match(token):
        return token, None
    # playlist with no video component
    if "list=" in token and "v=" not in token and "/watch" in token:
        return None, "PLAYLIST"
    for pat in (r"[?&]v=([A-Za-z0-9_-]{11})",
                r"youtu\.be/([A-Za-z0-9_-]{11})",
                r"/shorts/([A-Za-z0-9_-]{11})",
                r"/embed/([A-Za-z0-9_-]{11})",
                r"/live/([A-Za-z0-9_-]{11})"):
        m = re.search(pat, token)
        if m:
            return m.group(1), None
    if "list=" in token:
        return None, "PLAYLIST"
    return None, "INVALID_URL"


# ---------------------------------------------------------------------------
# Network ops
# ---------------------------------------------------------------------------
def yt_metadata(vid: str):
    """Lightweight metadata sweep (no subtitle download). Returns a dict; on
    failure the dict has an ``_error`` key. Retries once after a short pause."""
    m = _yt_metadata_once(vid)
    if m.get("_error"):
        time.sleep(random.uniform(2.0, 4.0))
        m = _yt_metadata_once(vid)
    return m


def _yt_metadata_once(vid: str):
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        p = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--dump-json", "--skip-download",
             "--no-warnings", url],
            env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=90)
    except Exception as e:
        return {"_error": repr(e)[:160]}
    if p.returncode != 0 or not p.stdout.strip():
        return {"_error": (p.stderr or "no stdout")[:160]}
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        return {"_error": f"json: {e}"}
    subs = set((d.get("subtitles") or {}).keys())
    auto = set((d.get("automatic_captions") or {}).keys())
    has_en = any(k == "en" or k.startswith("en-") for k in (subs | auto))
    return {
        "title": d.get("title"), "channel": d.get("channel"),
        "duration_seconds": int(d.get("duration") or 0),
        "upload_date": _fmt_date(d.get("upload_date")),
        "view_count": d.get("view_count"),
        "language": d.get("language"), "has_en": has_en,
        "is_live": bool(d.get("is_live")) or d.get("live_status") in ("is_live", "is_upcoming"),
    }


def _fmt_date(yyyymmdd):
    if yyyymmdd and len(yyyymmdd) == 8:
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return yyyymmdd


def run_fetchpy(claims_dir: Path, vid: str):
    """Call youtube-claims fetch.py --cache. Returns (exit, stdout, stderr)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    p = subprocess.run(
        [sys.executable, str(claims_dir / "scripts" / "fetch.py"), url, "--cache"],
        env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=180)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _looks_blocked(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in ("blocked", "429", "too many requests", "ipblocked"))


def yta_fetch(vid: str, meta: dict):
    """Pull the English track via youtube_transcript_api and write a
    fetch.py-shaped cache file. Returns (ok, error_text). Retries once after a
    pause when the error looks like rate-limiting."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi as A
    except Exception as e:
        return False, f"import: {e!r}"[:160]
    data = None
    err = ""
    for attempt in range(2):
        try:
            api = A()
            tr = api.list(vid).find_transcript(["en", "en-US", "en-GB"])
            data = [{"text": s.text, "start": s.start, "duration": s.duration}
                    for s in tr.fetch()]
            err = ""
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:200]
            if attempt == 0 and _looks_blocked(err):
                time.sleep(random.uniform(6.0, 10.0))
                continue
            break
    if not data:
        return False, err or "empty transcript"
    slug = re.sub(r"[^a-z0-9]+", "-", (meta.get("title") or vid).lower()).strip("-")[:55]
    out = {
        "video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
        "title": meta.get("title"), "channel": meta.get("channel"),
        "duration_seconds": meta.get("duration_seconds"),
        "view_count": meta.get("view_count"),
        "upload_date": meta.get("upload_date"), "slug": slug,
        "language": meta.get("language") or "en", "transcript": data,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_source": "yta",
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{vid}.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return True, ""


def read_cache_meta(vid: str):
    p = CACHE_DIR / f"{vid}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if "transcript" not in d or not isinstance(d["transcript"], list):
        return None  # likely a stored error payload, not a real transcript
    return d


# ---------------------------------------------------------------------------
# De-overlap: collapse YouTube rolling captions into clean, non-overlapping
# segments. yt-dlp's auto-caption path emits a sliding window where each cue
# repeats the tail of the previous one, so the raw transcript is ~3x larger
# than the words actually spoken. The inventory subagents (Phase B) read this
# file, so shrinking it cuts their token cost directly. It also makes quoting
# easier: each output segment is a clean contiguous chunk with no internal
# rolling repeats, so Pass 2 verbatim quotes have longer spans to match.
#
# It is a near no-op on already-clean manual subtitles (consecutive cues don't
# repeat, so the overlap is ~0). The overlap match is snapped to a word
# boundary so a coincidental phrase repeat can never clip a word mid-token.
# ---------------------------------------------------------------------------
def _norm_ws(s: str) -> str:
    return " ".join((s or "").split())


def deoverlap(segs, bucket_seconds: float = 14.0):
    """Return clean [{start,duration,text}] with rolling overlap removed and
    cues re-bucketed into ~bucket_seconds spans for quotable chunks."""
    words = []  # (word, start_seconds)
    acc = ""    # rolling tail of already-kept text, for overlap detection
    for seg in segs:
        t = _norm_ws(seg.get("text", ""))
        if not t:
            continue
        # Find the longest tail of what we have kept that the new cue repeats.
        # Both ends of the match must fall on word boundaries, and the match
        # must be at least MIN_OVERLAP_WORDS words. Without the boundary checks,
        # a cue starting "eta" after one ending "zeta" lost its first word; the
        # minimum stops a legitimately repeated single word ("very very good")
        # from being dropped. Real rolling captions repeat several words, so
        # this loses almost nothing; when unsure it keeps a duplicate instead
        # of deleting speech.
        maxk = min(len(acc), len(t))
        k = 0
        for j in range(maxk, 0, -1):
            if acc[-j:] != t[:j]:
                continue
            if j < len(acc) and acc[-j - 1] != " ":
                continue                        # match starts mid-word in what we kept
            if j < len(t) and t[j] != " ":
                continue                        # match ends mid-word in the new cue
            if len(t[:j].split()) < MIN_OVERLAP_WORDS:
                continue
            k = j
            break
        add = t[k:].strip()
        if not add:
            continue
        start = float(seg.get("start") or 0.0)
        for w in add.split():
            words.append((w, start))
        acc = (acc + " " + add)[-200:]
    if not words:
        return []
    out = []
    buf = []
    bstart = words[0][1]
    last = bstart
    for w, s in words:
        if buf and (s - bstart) >= bucket_seconds:
            out.append({"start": round(bstart, 3),
                        "duration": round(max(last - bstart, 0.0), 3),
                        "text": " ".join(buf)})
            buf = []
            bstart = s
        buf.append(w)
        last = s
    if buf:
        out.append({"start": round(bstart, 3),
                    "duration": round(max(last - bstart, 0.0), 3),
                    "text": " ".join(buf)})
    return out


def ensure_deoverlapped(vid: str) -> int:
    """Clean the cached transcript in place (idempotent). Returns the byte size
    of the transcript array that Phase B will read."""
    p = CACHE_DIR / f"{vid}.json"
    raw_p = CACHE_DIR / f"{vid}.raw.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0
    tr = d.get("transcript")
    if not isinstance(tr, list) or not tr:
        return 0
    if d.get("_deoverlap_version") == DEOVERLAP_VERSION:
        return len(json.dumps(tr, ensure_ascii=False))

    # Where does the raw caption text come from?
    #   1. a sidecar written the first time we processed this video (best: lets a
    #      fix to the cleaning code be re-applied without re-fetching), or
    #   2. a cache written by older code that had no sidecar: the raw text is gone,
    #      so leave it alone rather than clean already-cleaned text twice.
    if raw_p.exists():
        try:
            source = json.loads(raw_p.read_text(encoding="utf-8")).get("transcript") or tr
        except Exception:
            source = tr
    elif d.get("_deoverlapped"):
        return len(json.dumps(tr, ensure_ascii=False))     # legacy: processed, raw not kept
    else:
        source = tr
        raw_p.write_text(json.dumps({"video_id": vid, "transcript": tr}, ensure_ascii=False),
                         encoding="utf-8")

    clean = deoverlap(source)
    # Safety: never destroy content. Require the cleaned text to retain a
    # sane share of the raw's unique character mass; otherwise keep raw.
    raw_chars = len(_norm_ws(" ".join(s.get("text", "") for s in source)))
    clean_chars = len(" ".join(s["text"] for s in clean))
    if not clean or clean_chars < 0.25 * raw_chars:
        d["transcript"] = source
        d["_deoverlapped"] = True  # mark so we don't retry a no-win every run
        d["_deoverlap_skipped"] = True
        d["_deoverlap_version"] = DEOVERLAP_VERSION
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return len(json.dumps(source, ensure_ascii=False))
    d["transcript"] = clean
    d["_deoverlapped"] = True
    d["_deoverlap_version"] = DEOVERLAP_VERSION
    d["_raw_segments"] = len(source)
    d["_clean_segments"] = len(clean)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return len(json.dumps(clean, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Per-video worker
# ---------------------------------------------------------------------------
def process(token, claims_dir, min_seconds, gate, log, deoverlap_on=True):
    def tbytes(vid, cached):
        if deoverlap_on:
            return ensure_deoverlapped(vid)
        return len(json.dumps(cached.get("transcript", []), ensure_ascii=False))

    vid, reason = extract_id(token)
    if reason:
        log(video=token, ok=0, method="none", status=reason)
        return {"input": token, "video_id": None, "status": reason, "method": "none"}

    t0 = time.time()
    # 1) cache hit -> no network
    cached = read_cache_meta(vid)
    if cached:
        tb = tbytes(vid, cached)
        log(video=vid, ok=1, method="cache_hit", http_429=0, retries=0,
            transcript_bytes=tb,
            seconds=round(time.time() - t0, 2), status="ok")
        return _ledger(vid, cached, "ok", "cache_hit")

    http_429 = 0
    retries = 0
    status = "NO_TRANSCRIPT"

    # 2) cheap metadata sweep: reject live / too short / non-English before any
    #    transcript request, and get the title/channel/duration for the ledger.
    with gate:
        meta = yt_metadata(vid)
        time.sleep(random.uniform(0.3, 1.0))
    meta_ok = bool(meta) and not meta.get("_error")
    if meta_ok:
        lang = (meta.get("language") or "").lower()
        reject = None
        if meta.get("is_live"):
            reject = "LIVE_STREAM"
        elif (meta.get("duration_seconds") or 0) and meta["duration_seconds"] < min_seconds:
            reject = "TOO_SHORT"
        elif lang and not lang.startswith("en"):
            reject = "NON_ENGLISH"
        if reject:
            log(video=vid, ok=0, method="none", http_429=0, retries=0,
                status=reject, seconds=round(time.time() - t0, 2))
            return _ledger(vid, meta, reject, "none")

    # 3) PRIMARY: subtitle API. In practice yt-dlp's subtitle path (used by
    #    fetch.py) is the one that gets HTTP 429, so try this first.
    yta_err = ""
    if meta_ok:
        with gate:
            ok, yta_err = yta_fetch(vid, meta)
            time.sleep(random.uniform(0.3, 1.0))
        if ok:
            cached = read_cache_meta(vid) or meta
            tb = tbytes(vid, cached) if cached.get("transcript") else 0
            log(video=vid, ok=1, method="yta", http_429=0, retries=0,
                transcript_bytes=tb,
                seconds=round(time.time() - t0, 2), status="ok")
            return _ledger(vid, cached, "ok", "yta")

    # 4) BACKUP: youtube-claims fetch.py with bounded concurrency + 429 backoff.
    with gate:
        rc, out, err = run_fetchpy(claims_dir, vid)
        blob = (out + err)
        while rc != 0 and "429" in blob and retries < 2:
            http_429 += 1
            retries += 1
            time.sleep(min(8 * (2 ** (retries - 1)) + random.uniform(0, 3), 30))
            rc, out, err = run_fetchpy(claims_dir, vid)
            blob = (out + err)
    if rc == 0:
        cached = read_cache_meta(vid)
        if cached:
            tb = tbytes(vid, cached)
            log(video=vid, ok=1, method="fetchpy", http_429=http_429,
                retries=retries, transcript_bytes=tb,
                seconds=round(time.time() - t0, 2), status="ok")
            return _ledger(vid, cached, "ok", "fetchpy")

    for line in (err + "\n" + out).splitlines():
        line = line.strip()
        if line.startswith("{") and '"error"' in line:
            try:
                status = json.loads(line).get("error", status)
            except Exception:
                pass
    log(video=vid, ok=0, method="none", http_429=http_429, retries=retries,
        status=status, seconds=round(time.time() - t0, 2))
    base = meta if meta_ok else {}
    return _ledger(vid, base, status, "none")


def _ledger(vid, meta, status, method):
    return {
        "video_id": vid,
        "title": meta.get("title"),
        "channel": meta.get("channel"),
        "duration_seconds": meta.get("duration_seconds"),
        "upload_date": meta.get("upload_date"),
        "language": meta.get("language"),
        "status": status,
        "method": method,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claims-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--concurrency", type=int, default=3,
                    help="max simultaneous network fetches (default 3)")
    ap.add_argument("--min-seconds", type=int, default=16,
                    help="reject videos shorter than this (default 16)")
    ap.add_argument("--no-deoverlap", action="store_true",
                    help="keep raw rolling-caption transcripts (skip the "
                         "de-overlap cleanup that shrinks what Phase B reads)")
    ap.add_argument("--telemetry-dir",
                    default=str(Path.home() / "youtube-reports" / ".telemetry"))
    ap.add_argument("videos", nargs="+")
    args = ap.parse_args()

    claims_dir = Path(args.claims_dir)
    if not (claims_dir / "scripts" / "fetch.py").exists():
        sys.stderr.write(f"prefetch: fetch.py not found under {claims_dir}\n")
        return 2

    log = make_logger(args.run_id, args.telemetry_dir)
    # de-dupe inputs, preserve order
    seen = set()
    tokens = [t for t in args.videos if not (t in seen or seen.add(t))]

    gate = threading.Semaphore(max(1, args.concurrency))
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(process, t, claims_dir, args.min_seconds, gate, log,
                          not args.no_deoverlap): t
                for t in tokens}
        for f in as_completed(futs):
            t = futs[f]
            try:
                results[t] = f.result()
            except Exception as e:
                results[t] = {"input": t, "video_id": None,
                              "status": f"ERROR: {repr(e)[:120]}", "method": "none"}

    ledger = [results[t] for t in tokens]
    ok = [r for r in ledger if r["status"] == "ok"]
    rej = [r for r in ledger if r["status"] != "ok"]
    sys.stderr.write(
        f"prefetch: {len(ok)}/{len(ledger)} ready "
        f"({', '.join(sorted({r['method'] for r in ok})) or 'none'}); "
        f"{len(rej)} rejected"
        + (": " + ", ".join(f"{r.get('video_id') or r.get('input')}={r['status']}"
                            for r in rej) if rej else "") + "\n")
    json.dump({"run_id": args.run_id, "ready": [r["video_id"] for r in ok],
               "videos": ledger}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
