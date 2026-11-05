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
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / "youtube-reports" / ".cache"
EXPECTED_FILES = ["prefetch.py", "telemetry.py", "rank_videos.py", "rank_tools.py",
                  "inventory_brief.md", "factcheck_brief.md", "VERSION"]
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
            out[vid] = {"error": "not cached"}
            continue
        d = _load(p)
        text = " ".join(s.get("text", "") for s in d.get("transcript", []))
        out[vid] = scan_text(text)
    print(json.dumps(out, indent=1, sort_keys=True))
    if args.run_id:
        total = sum(v.get("hits", 0) for v in out.values())
        flagged = sum(1 for v in out.values() if v.get("hits", 0))
        try:
            subprocess.run([sys.executable, "-I", str(HERE / "telemetry.py"), "log", "--run-id", args.run_id,
                            "--stage", "fetch", "--event", "injection_scan", "--kv",
                            f"videos={len(out)}", f"suspect_videos={flagged}", f"suspect_hits={total}"],
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
    vague = pitches = 0
    items = {}   # seconds -> count of concrete/evidence items at that timestamp
    for sec in by_section.values():
        vague += len(sec.get("vague_claims", []))
        pitches += len(sec.get("pitches", []))
        for kind in ("concrete_claims", "evidence_shown"):
            for it in sec.get(kind, []):
                s = _ts_to_seconds(it.get("timestamp", ""))
                if s is not None:
                    items[s] = items.get(s, 0) + 1
    return {"title": meta.get("title", ""), "duration_s": int(meta.get("duration_seconds") or 0),
            "vague": vague, "pitches": pitches, "items": items}


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

    if errors:
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, indent=1))
        return 2

    rank_input = {
        "meta": {"goal": kp.get("goal", ""), "built_at": today.isoformat()},
        "videos": [{"id": vid, "title": inv[vid]["title"], "duration_s": inv[vid]["duration_s"],
                    "vague": inv[vid]["vague"], "pitches": inv[vid]["pitches"],
                    "relevance": float(videos_cfg[vid].get("relevance", 1.0)),
                    "points": members[vid]} for vid in inv],
        "points": [{"id": p["id"], "core": bool(p.get("core")),
                    "verdict": verdict_by_id.get(p["id"], "NOT_CHECKED")} for p in points],
    }
    (adir / "rank_input.json").write_text(json.dumps(rank_input, indent=1), encoding="utf-8")
    summary = {"ok": True, "errors": [], "warnings": warnings, "coverage": coverage,
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

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
