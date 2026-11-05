#!/usr/bin/env python3
"""Checkable helpers for the steps of youtube-video-ranker that used to be done by hand.

  check-install
      Verify this skill's own files are present. Exit 1 if any are missing. (A skill
      uploaded as a single SKILL.md arrives without its scripts; this makes that loud.)

  scan-injection VIDEO_ID [VIDEO_ID ...] [--run-id R]
      Count instruction-like strings aimed at an AI inside cached transcripts. A
      suspect hit is a prompt to look, not a verdict: tutorials about prompting
      quote these phrases legitimately. With --run-id, logs an event to telemetry.

  build-input --analysis DIR [--cache-dir DIR] [--score]
      Build rank_input.json from the persisted analysis (keypoints.json and
      verdicts.json in DIR) plus the cached inventories, checking every link:
      each key point must point at a real video and a real inventory item, each
      verdict at a real key point. vague/pitch counts and durations come from the
      caches, not from hand tallies. With --score, also runs rank_videos.py and
      writes rank_output.json. Exit 2 on errors.

  plan-inventory VIDEO_ID [VIDEO_ID ...] [--short-minutes 10] [--max-batch 3]
                 [--max-total-minutes 24] [--max-words 6000]
      Group the cached videos into inventory-agent batches. Each agent costs about
      70K tokens of fixed overhead, so short videos share an agent; long ones stay
      solo. --max-batch 1 turns batching off. Prints {"batches": [[id, ...], ...]}.

  check-report --analysis DIR --report FILE
      Compare a written report with rank_output.json, the verdicts and the caches:
      table order, scores, counts, lengths and checked tallies, watch-plan links, every
      outside URL, verdict tallies, "N of M key points" statements, the pipeline
      version and the required sections. Exit 2 on errors. It does not read prose.

All commands are deterministic and use only the standard library.

keypoints.json:
  {"goal": "...",
   "videos": {"<video_id>": {"relevance": 1.0, "reason": ""}},
   "points": [{"id": "K1", "text": "...", "core": true,
               "refs": [{"video": "<video_id>", "ts": "2:08"}]}]}
verdicts.json:
  [{"id": "K1", "verdict": "SUPPORTED", "source_url": "...", "reason": "...",
    "checked_at": "2026-11-04", "searches": 1, "cache_hit": 0}]
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / "youtube-reports" / ".cache"
EXPECTED_FILES = ["prefetch.py", "telemetry.py", "rank_videos.py", "rank_tools.py",
                  "inventory_brief.md", "factcheck_brief.md", "VERSION"]
MIN_SEGMENT_S = 90   # watch-plan segments shorter than this are joined to a neighbour
VERDICTS = {"SUPPORTED", "PARTLY", "OUTDATED", "CONTRADICTED", "UNVERIFIED", "NOT_CHECKED"}
CHECKED = VERDICTS - {"NOT_CHECKED", "UNVERIFIED"}

# Instruction-like phrases aimed at an AI. Deliberately narrow; hits are suspects only.
INJECTION_PATTERNS = [
    (r"\b(?:ignore|disregard|forget)\b[^.]{0,30}\b(?:previous|prior|above|earlier|all|your)\b[^.]{0,20}\b(?:instructions?|prompts?|rules)\b",
     "ignore-instructions"),
    (r"\b(?:rate|score|rank)\s+this\s+video\b[^.]{0,20}\b(?:\d+|ten|highly|first|best)\b", "rate-this-video"),
    (r"\b(?:system|developer)\s+(?:prompt|message)\b", "system-prompt"),
    (r"\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|language model|chatbot|llm)\b", "you-are-an-ai"),
    (r"\bas\s+an\s+ai\s+(?:language\s+)?(?:model|assistant)\b", "as-an-ai"),
    (r"\b(?:new|updated)\s+instructions\b", "new-instructions"),
    (r"\bdo\s+not\s+(?:tell|reveal|mention)\b[^.]{0,30}\b(?:user|human)\b", "hide-from-user"),
]


def _ts_to_seconds(ts) -> int | None:
    parts = str(ts).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ check-install
def cmd_check_install(args) -> int:
    missing = [f for f in EXPECTED_FILES if not (HERE / f).exists()]
    version = (HERE / "VERSION").read_text(encoding="utf-8").strip() if (HERE / "VERSION").exists() else None
    print(json.dumps({"ok": not missing, "version": version, "scripts_dir": str(HERE),
                      "missing": missing}))
    return 0 if not missing else 1


# ------------------------------------------------------------------ scan-injection
def scan_text(text: str) -> dict:
    low = re.sub(r"\s+", " ", text.lower().replace("’", "'"))
    hits, examples = {}, []
    for pat, label in INJECTION_PATTERNS:
        for m in re.finditer(pat, low):
            hits[label] = hits.get(label, 0) + 1
            if len(examples) < 3:
                examples.append(low[max(0, m.start() - 30): m.end() + 30])
    return {"hits": sum(hits.values()), "by_pattern": hits, "examples": examples}


def cmd_scan_injection(args) -> int:
    cache = Path(args.cache_dir)
    out = {}
    for vid in args.videos:
        p = cache / f"{vid}.json"
        if not p.exists():
            out[vid] = {"error": "not cached", "scanned": False}
            continue
        d = _load(p)
        segs = d.get("transcript") if isinstance(d, dict) else None
        text = " ".join(s.get("text", "") for s in segs or [])
        if not text.strip():
            # A failed fetch is cached as an error payload with no transcript. Scanning
            # nothing and printing "0 hits" would read as "checked and clean".
            out[vid] = {"scanned": False,
                        "error": f"no transcript ({d.get('error', 'empty')}), nothing was scanned"}
            continue
        out[vid] = {"scanned": True, **scan_text(text)}
    print(json.dumps(out, indent=1, sort_keys=True))
    if args.run_id:
        total = sum(v.get("hits", 0) for v in out.values())
        flagged = sum(1 for v in out.values() if v.get("hits", 0))
        unscanned = sum(1 for v in out.values() if not v.get("scanned"))
        try:
            subprocess.run([sys.executable, "-I", str(HERE / "telemetry.py"), "log", "--run-id", args.run_id,
                            "--stage", "fetch", "--event", "injection_scan", "--kv",
                            f"videos={len(out)}", f"suspect_videos={flagged}", f"suspect_hits={total}",
                            f"unscanned={unscanned}"],
                           capture_output=True, timeout=20)
        except Exception:
            pass
    return 0


# ------------------------------------------------------------------ build-input
def inventory_summary(cache: Path, vid: str):
    meta_p, p2_p = cache / f"{vid}.json", cache / f"{vid}-pass2.json"
    if not meta_p.exists() or not p2_p.exists():
        return None
    meta = _load(meta_p)
    by_section = _load(p2_p).get("output", {}).get("by_section", {})
    vague = pitches = concrete = evidence = 0
    items = {}   # seconds -> count of concrete/evidence items at that timestamp
    for sec in by_section.values():
        vague += len(sec.get("vague_claims", []))
        pitches += len(sec.get("pitches", []))
        concrete += len(sec.get("concrete_claims", []))
        evidence += len(sec.get("evidence_shown", []))
        for kind in ("concrete_claims", "evidence_shown"):
            for it in sec.get(kind, []):
                s = _ts_to_seconds(it.get("timestamp", ""))
                if s is not None:
                    items[s] = items.get(s, 0) + 1
    return {"title": meta.get("title", ""), "duration_s": int(meta.get("duration_seconds") or 0),
            "vague": vague, "pitches": pitches, "concrete": concrete, "evidence": evidence,
            "items": items, "sections": _pass1_sections(cache, vid)}


def _pass1_sections(cache: Path, vid: str) -> list:
    """Pass 1 sections as [{start_s, end_s, type}] in time order; [] if unavailable."""
    p = cache / f"{vid}-pass1.json"
    if not p.exists():
        return []
    try:
        raw = _load(p).get("output", {}).get("sections", [])
    except Exception:
        return []
    out = []
    for s in raw:
        a, b = _ts_to_seconds(s.get("start", "")), _ts_to_seconds(s.get("end", ""))
        if a is not None and b is not None and b > a:
            out.append({"start_s": a, "end_s": b, "type": s.get("type", "")})
    return sorted(out, key=lambda s: s["start_s"])


def build_segments(sections: list, refs: list) -> list:
    """Group key-point references into the Pass 1 sections that contain them.

    refs: [(point_id, seconds)]. A segment is a section holding at least one reference.
    A reference after the last section's end extends that section (captions and
    section ends are not exact), by 20 s. Returns [] if there are no sections.
    """
    if not sections:
        return []
    segs = {}
    for pid, t in refs:
        idx = next((i for i, s in enumerate(sections) if s["start_s"] <= t < s["end_s"]), None)
        if idx is None:
            before = [i for i, s in enumerate(sections) if s["start_s"] <= t]
            idx = before[-1] if before else 0
        seg = segs.setdefault(idx, {"start_s": sections[idx]["start_s"],
                                    "end_s": sections[idx]["end_s"], "points": set()})
        seg["points"].add(pid)
        seg["end_s"] = max(seg["end_s"], t + 20 if t >= sections[idx]["end_s"] else 0)
    out = [dict(s) for _, s in sorted(segs.items())]
    # A tiny section is not worth a separate recommendation: join it to its nearer neighbour.
    while len(out) > 1:
        i = next((k for k, s in enumerate(out) if s["end_s"] - s["start_s"] < MIN_SEGMENT_S), None)
        if i is None:
            break
        gaps = []
        if i > 0:
            gaps.append((out[i]["start_s"] - out[i - 1]["end_s"], i - 1))
        if i < len(out) - 1:
            gaps.append((out[i + 1]["start_s"] - out[i]["end_s"], i + 1))
        j = min(gaps)[1]
        a, b = sorted((i, j))
        out[a] = {"start_s": out[a]["start_s"], "end_s": max(out[a]["end_s"], out[b]["end_s"]),
                  "points": out[a]["points"] | out[b]["points"]}
        del out[b]
    return [{"start_s": s["start_s"], "end_s": s["end_s"],
             "points": sorted(s["points"], key=lambda i: (len(i), i))} for s in out]


def apply_ranges(segments: list, ranges: list, vid: str, errors: list) -> list:
    """Down-weight segments that mostly sit inside a range the orchestrator marked off-goal.

    keypoints.json: "videos": {id: {"relevance": 1.0, "ranges": [{"start": "22:11",
    "end": "34:27", "relevance": 0.2, "reason": "pistol red dot, not a rifle"}]}}
    Video-level relevance cannot say "the first 22 minutes are on topic, the rest is
    not". A segment gets the relevance of the range that covers at least half of it.
    """
    parsed = []
    for r in ranges or []:
        a, b = _ts_to_seconds(r.get("start", "")), _ts_to_seconds(r.get("end", ""))
        rel = r.get("relevance")
        if a is None or b is None or b <= a:
            errors.append(f"video {vid}: range {r.get('start')!r} to {r.get('end')!r} is not a valid time range")
        elif not isinstance(rel, (int, float)) or isinstance(rel, bool) or not 0.0 <= rel <= 1.0:
            errors.append(f"video {vid}: range {r.get('start')} has relevance {rel!r}, expected 0 to 1")
        else:
            parsed.append((a, b, float(rel)))
    out = []
    for s in segments:
        length = s["end_s"] - s["start_s"]
        rel = 1.0
        for a, b, rr in parsed:
            overlap = min(b, s["end_s"]) - max(a, s["start_s"])
            if length > 0 and overlap / length >= 0.5:
                rel = min(rel, rr)
        out.append({**s, "relevance": rel} if rel < 1.0 else s)
    return out


def cmd_build_input(args) -> int:
    adir, cache = Path(args.analysis), Path(args.cache_dir)
    errors, warnings = [], []
    try:
        kp = _load(adir / "keypoints.json")
    except Exception as e:
        print(json.dumps({"ok": False, "errors": [f"cannot read keypoints.json: {e}"]}))
        return 2
    try:
        verdicts = _load(adir / "verdicts.json") if (adir / "verdicts.json").exists() else []
    except Exception as e:
        errors.append(f"cannot read verdicts.json: {e}")
        verdicts = []

    videos_cfg = kp.get("videos", {})
    points = kp.get("points", [])
    inv = {}
    for vid in videos_cfg:
        s = inventory_summary(cache, vid)
        if s is None:
            errors.append(f"video {vid}: no cached transcript/inventory in {cache}")
        else:
            inv[vid] = s

    seen_ids = set()
    mapped = {vid: set() for vid in inv}
    members = {vid: [] for vid in inv}
    ref_secs = {vid: [] for vid in inv}   # (point id, seconds) for the segment builder
    for p in points:
        pid = p.get("id")
        if not pid or pid in seen_ids:
            errors.append(f"missing or duplicate key point id {pid!r}")
            continue
        seen_ids.add(pid)
        if not p.get("refs"):
            errors.append(f"key point {pid} has no refs, so no video makes it")
        for ref in p.get("refs", []):
            vid = ref.get("video")
            if vid not in videos_cfg:
                errors.append(f"key point {pid} refs unknown video {vid!r}")
                continue
            if vid not in inv:
                continue
            if pid not in members[vid]:
                members[vid].append(pid)
            sec = _ts_to_seconds(ref.get("ts", ""))
            if sec is None:
                errors.append(f"key point {pid} has an unreadable timestamp {ref.get('ts')!r}")
            elif sec in inv[vid]["items"]:
                mapped[vid].add(sec)
                ref_secs[vid].append((pid, sec))
            else:
                warnings.append(f"key point {pid}: no inventory item at {ref.get('ts')} in {vid}")

    verdict_by_id = {}
    today = datetime.date.today()
    ages = {}
    for v in verdicts:
        vid_ = v.get("id")
        if vid_ not in seen_ids:
            errors.append(f"verdict for unknown key point {vid_!r}")
            continue
        if v.get("verdict") not in VERDICTS:
            errors.append(f"verdict for {vid_} is invalid: {v.get('verdict')!r}")
            continue
        verdict_by_id[vid_] = v["verdict"]
        if v["verdict"] in CHECKED:
            if not v.get("source_url"):
                warnings.append(f"verdict for {vid_} has no source_url")
            if not v.get("checked_at"):
                warnings.append(f"verdict for {vid_} has no checked_at date")
            else:
                try:
                    ages[vid_] = (today - datetime.date.fromisoformat(v["checked_at"])).days
                except ValueError:
                    warnings.append(f"verdict for {vid_} has an unreadable checked_at {v['checked_at']!r}")

    coverage = {}
    for vid, s in inv.items():
        total = len(s["items"])
        coverage[vid] = {"items": total, "mapped": len(mapped[vid]),
                         "pct": round(100 * len(mapped[vid]) / total, 1) if total else None}

    segments = {vid: build_segments(inv[vid]["sections"], ref_secs[vid]) for vid in inv}
    for vid in inv:
        segments[vid] = apply_ranges(segments[vid], videos_cfg[vid].get("ranges", []), vid, errors)

    if errors:
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, indent=1))
        return 2

    rank_input = {
        "meta": {"goal": kp.get("goal", ""), "built_at": today.isoformat()},
        "videos": [dict({"id": vid, "title": inv[vid]["title"], "duration_s": inv[vid]["duration_s"],
                         "vague": inv[vid]["vague"], "pitches": inv[vid]["pitches"],
                         "relevance": float(videos_cfg[vid].get("relevance", 1.0)),
                         "points": members[vid]},
                        **({"segments": segments[vid]} if segments[vid] else {}))
                   for vid in inv],
        "points": [{"id": p["id"], "core": bool(p.get("core")),
                    "verdict": verdict_by_id.get(p["id"], "NOT_CHECKED")} for p in points],
    }
    (adir / "rank_input.json").write_text(json.dumps(rank_input, indent=1), encoding="utf-8")
    # Counts for the report to COPY, so prose numbers are never tallied by hand.
    facts = {}
    for vid, s in inv.items():
        total_items = s["concrete"] + s["evidence"] + s["vague"] + s["pitches"]
        noise = s["vague"] + s["pitches"]
        facts[vid] = {"title": s["title"], "duration_s": s["duration_s"],
                      "concrete": s["concrete"], "evidence": s["evidence"],
                      "vague": s["vague"], "pitches": s["pitches"],
                      "inventory_items": total_items, "vague_or_pitch_items": noise,
                      "vague_or_pitch_pct": round(100 * noise / total_items, 1) if total_items else None,
                      "key_points": len(members[vid]),
                      "segments": segments[vid]}
    summary = {"ok": True, "errors": [], "warnings": warnings, "coverage": coverage,
               "facts": facts,
               "verdict_age_days_max": max(ages.values()) if ages else None,
               "rank_input": str(adir / "rank_input.json"), "rank_output": None}
    if args.score:
        r = subprocess.run([sys.executable, "-I", str(HERE / "rank_videos.py")],
                           input=json.dumps(rank_input), capture_output=True, text=True)
        if r.returncode != 0:
            summary.update({"ok": False, "errors": [r.stderr.strip()]})
            print(json.dumps(summary, indent=1))
            return 2
        (adir / "rank_output.json").write_text(r.stdout, encoding="utf-8")
        summary["rank_output"] = str(adir / "rank_output.json")
    print(json.dumps(summary, indent=1))
    return 0


# ------------------------------------------------------------------ plan-inventory
def plan_batches(videos: dict, short_s: int, max_batch: int, max_total_s: int, max_words: int) -> list:
    """Group short videos so one inventory agent handles several; long ones stay solo.

    videos: {id: {"duration_s": int, "words": int}}. Each inventory agent pays about
    70K tokens of fixed overhead whatever the video, so a 2-minute video that gets
    its own agent costs about as much as a 20-minute one. First-fit-decreasing,
    capped on video count, total minutes and total transcript words.
    """
    longs = [v for v, m in videos.items() if m["duration_s"] > short_s or max_batch <= 1]
    shorts = sorted((v for v in videos if v not in longs),
                    key=lambda v: videos[v]["duration_s"], reverse=True)
    batches = []   # each: [ids, total_s, total_words]
    for v in shorts:
        m = videos[v]
        for b in batches:
            if (len(b[0]) < max_batch and b[1] + m["duration_s"] <= max_total_s
                    and b[2] + m["words"] <= max_words):
                b[0].append(v); b[1] += m["duration_s"]; b[2] += m["words"]
                break
        else:
            batches.append([[v], m["duration_s"], m["words"]])
    solo = sorted(longs, key=lambda v: videos[v]["duration_s"], reverse=True)
    return [[v] for v in solo] + [b[0] for b in batches]


def cmd_plan_inventory(args) -> int:
    cache = Path(args.cache_dir)
    videos, problems = {}, {}
    for vid in args.videos:
        p = cache / f"{vid}.json"
        if not p.exists():
            problems[vid] = "not cached"
            continue
        d = _load(p)
        segs = d.get("transcript") if isinstance(d, dict) else None
        if not segs:
            problems[vid] = "no transcript"
            continue
        videos[vid] = {"duration_s": int(d.get("duration_seconds") or 0),
                       "words": sum(len(s.get("text", "").split()) for s in segs)}
    batches = plan_batches(videos, int(args.short_minutes * 60), args.max_batch,
                           int(args.max_total_minutes * 60), args.max_words)
    print(json.dumps({"batches": batches, "agents": len(batches), "videos": len(videos),
                      "agents_without_batching": len(videos), "skipped": problems,
                      "settings": {"short_minutes": args.short_minutes, "max_batch": args.max_batch,
                                   "max_total_minutes": args.max_total_minutes,
                                   "max_words": args.max_words}}, indent=1))
    return 0


# ------------------------------------------------------------------ check-report
REPORT_SECTIONS = [("recommendation", r"recommendation"), ("ranking", r"ranking"),
                   ("watch plan", r"watch plan"), ("conflicts", r"conflict|contradiction"),
                   ("per-video notes", r"notes"), ("left out", r"left out"), ("limits", r"limits")]
VERDICT_WORDS = ("SUPPORTED", "PARTLY", "OUTDATED", "CONTRADICTED", "UNVERIFIED")
URL_RE = re.compile(r"https?://[^\s)>\]|]+")
YT_LINK_RE = re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})(?:\?t=(\d+))?")
NUM_RE = re.compile(r"(?<![\w.:/])(\d+(?:\.\d+)?)(?![\w.:/])")


def _norm_url(u: str) -> str:
    return u.rstrip(".,;:").rstrip("/").lower()


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def check_report(report: str, ro: dict, ri: dict, kp: dict, verdicts: list, version: str,
                 cache: Path | None = None):
    """Compare a written report with the numbers the pipeline produced. Returns (errors, warnings).

    Covers the ranking table (order, score, counts, length, checked tallies), watch-plan
    links, every outside URL, verdict tallies, "N of M key points" statements, the pipeline
    version and the required sections. It does not read prose, so numbers in sentences must
    still be copied from `build-input`'s `facts` rather than tallied by hand.
    """
    errors, warnings = [], []
    lines = report.splitlines()

    heads = [ln.lstrip("# ").lower() for ln in lines if ln.startswith("#")]
    for name, pat in REPORT_SECTIONS:
        if not any(re.search(pat, h) for h in heads):
            errors.append(f"report has no '{name}' section")

    if version and version not in report:
        errors.append(f"report does not state the pipeline version {version}")

    vid_info = {v["id"]: v for v in ri.get("videos", [])}
    vpoints = {p["id"]: p for p in ri.get("points", [])}
    vmap = {v.get("id"): v for v in verdicts}

    # ---- ranking table
    table, in_tbl = [], False
    for ln in lines:
        if ln.lstrip().startswith("|"):
            low = ln.lower()
            if not in_tbl and "score" in low and "rank" in low:
                in_tbl = True
                continue
            if in_tbl and not set(ln.replace("|", "").strip()) <= set("-: "):
                table.append(ln)
        elif in_tbl:
            break
    if not table:
        errors.append("no ranking table found (a table whose header has Rank and Score)")
    ranked = ro.get("ranked", [])
    for pos, r in enumerate(ranked, start=1):
        vid = r["id"]
        row = next((ln for ln in table if vid in ln), None)
        if row is None:
            errors.append(f"ranking table has no row for {vid} (link the title to https://youtu.be/{vid})")
            continue
        if table.index(row) != pos - 1:
            errors.append(f"{vid} is ranked {pos} but sits in row {table.index(row) + 1} of the table")
        cells = _cells(row)
        if cells and cells[0].isdigit() and int(cells[0]) != pos:
            errors.append(f"{vid}: rank cell says {cells[0]}, ranking says {pos}")
        nums = set(NUM_RE.findall(re.sub(r"\(?https?://\S+", " ", row)))
        info = vid_info.get(vid, {})
        want = {"score": f"{r['score']:.1f}", "key points": str(r["points"]),
                "vague claims": str(info.get("vague", "")), "pitches": str(info.get("pitches", "")),
                "relevance": f"{r['relevance']:.1f}"}
        for label, tok in want.items():
            if tok and tok not in nums:
                errors.append(f"{vid}: table row does not show {label} {tok}")
        dur = int(info.get("duration_s") or 0)
        times = re.findall(r"\b(\d+):(\d{2})\b", re.sub(r"https?://\S+", " ", row))
        if dur and times:
            secs = [int(m) * 60 + int(s) for m, s in times]
            if not any(abs(s - dur) <= 1 for s in secs):
                errors.append(f"{vid}: table length is {times[0][0]}:{times[0][1]}, "
                              f"the video is {dur // 60}:{dur % 60:02d}")
        ids = info.get("points", [])
        tally = [sum(1 for i in ids if vpoints.get(i, {}).get("verdict") == w)
                 for w in ("SUPPORTED", "PARTLY", "CONTRADICTED")]
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", row)
        if m and [int(x) for x in m.groups()] != tally:
            errors.append(f"{vid}: checked tally in the table is {'/'.join(m.groups())}, "
                          f"verdicts give {'/'.join(map(str, tally))}")
        elif not m:
            warnings.append(f"{vid}: table row has no supported / partly / contradicted tally")

    # ---- watch-plan links
    cache = cache or CACHE_DIR
    plan_ranges = {w["id"]: w["ranges"] for w in ro.get("watch_ranges", [])}
    for vid, t in YT_LINK_RE.findall(report):
        if vid not in vid_info:
            errors.append(f"link to {vid}, which is not a ranked video")
            continue
        if not t:
            continue
        t = int(t)
        dur = int(vid_info[vid].get("duration_s") or 0)
        if dur and t > dur:
            errors.append(f"link {vid}?t={t} points past the end of the video ({dur}s)")
            continue
        s = inventory_summary(cache, vid) if cache.exists() else None
        if s and s["items"] and t not in s["items"]:
            warnings.append(f"link {vid}?t={t} matches no inventory item")
        if vid in plan_ranges and not any(a <= t <= b for a, b in plan_ranges[vid]):
            warnings.append(f"link {vid}?t={t} is outside the planned watch range")
        elif vid not in plan_ranges:
            warnings.append(f"link {vid}?t={t} is into a video the watch plan skipped")

    # ---- outside URLs and verdict tallies
    known = {_norm_url(v["source_url"]) for v in verdicts if v.get("source_url")}
    for u in sorted({_norm_url(x) for x in URL_RE.findall(report)}):
        if "youtu.be/" in u or "youtube.com/" in u:
            continue
        if u not in known:
            errors.append(f"URL in the report is not a source of any verdict: {u}")
    shown = {w: len(re.findall(rf"\|\s*{w}\s*\|", report)) for w in VERDICT_WORDS}
    actual = Counter(v.get("verdict") for v in verdicts)
    for w in VERDICT_WORDS:
        if shown[w] > actual.get(w, 0):
            errors.append(f"report shows {shown[w]} {w} verdicts, the analysis has {actual.get(w, 0)}")
    if shown["CONTRADICTED"] < actual.get("CONTRADICTED", 0):
        errors.append("a CONTRADICTED verdict is missing from the report's conflicts table")

    # ---- "N of M key points"
    n_checked = sum(1 for v in verdicts if v.get("verdict") in VERDICTS - {"NOT_CHECKED"})
    n_points = len(kp.get("points", []))
    for a, b in re.findall(r"(\d+)\s+of\s+(?:the\s+)?(\d+)\s+key\s+points", report):
        if (int(a), int(b)) != (n_checked, n_points):
            errors.append(f"report says '{a} of {b} key points', the analysis has {n_checked} of {n_points}")
    return errors, warnings


def cmd_check_report(args) -> int:
    adir = Path(args.analysis)
    try:
        report = Path(args.report).read_text(encoding="utf-8")
        ro, ri, kp = (_load(adir / n) for n in ("rank_output.json", "rank_input.json", "keypoints.json"))
        verdicts = _load(adir / "verdicts.json") if (adir / "verdicts.json").exists() else []
        version = (HERE / "VERSION").read_text(encoding="utf-8").strip()
    except Exception as e:
        print(json.dumps({"ok": False, "errors": [f"cannot read inputs: {e}"], "warnings": []}))
        return 2
    errors, warnings = check_report(report, ro, ri, kp, verdicts, version, Path(args.cache_dir))
    print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, indent=1))
    return 0 if not errors else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-install").set_defaults(func=cmd_check_install)

    si = sub.add_parser("scan-injection")
    si.add_argument("videos", nargs="+")
    si.add_argument("--cache-dir", default=str(CACHE_DIR))
    si.add_argument("--run-id", default=None)
    si.set_defaults(func=cmd_scan_injection)

    bi = sub.add_parser("build-input")
    bi.add_argument("--analysis", required=True)
    bi.add_argument("--cache-dir", default=str(CACHE_DIR))
    bi.add_argument("--score", action="store_true")
    bi.set_defaults(func=cmd_build_input)

    pi_ = sub.add_parser("plan-inventory")
    pi_.add_argument("videos", nargs="+")
    pi_.add_argument("--cache-dir", default=str(CACHE_DIR))
    pi_.add_argument("--short-minutes", type=float, default=10.0)
    pi_.add_argument("--max-batch", type=int, default=3, help="1 turns batching off")
    pi_.add_argument("--max-total-minutes", type=float, default=24.0)
    pi_.add_argument("--max-words", type=int, default=6000)
    pi_.set_defaults(func=cmd_plan_inventory)

    cr = sub.add_parser("check-report")
    cr.add_argument("--analysis", required=True)
    cr.add_argument("--report", required=True)
    cr.add_argument("--cache-dir", default=str(CACHE_DIR))
    cr.set_defaults(func=cmd_check_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
