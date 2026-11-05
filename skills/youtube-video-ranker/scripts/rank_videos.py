#!/usr/bin/env python3
"""Score and rank videos on one topic. Reads JSON on stdin, prints JSON.

    python3 -I rank_videos.py < rank_input.json > rank_output.json

Input shape:
{
  "videos": [
    {"id": "abc", "title": "T", "duration_s": 900, "vague": 4, "pitches": 1,
     "relevance": 1.0, "points": ["K1", "K2"]}
  ],
  "points": [
    {"id": "K1", "core": true,
     "verdict": "SUPPORTED|PARTLY|OUTDATED|CONTRADICTED|UNVERIFIED|NOT_CHECKED"}
  ]
}
"points" on a video lists the canonical key point ids that video makes.
"relevance" (0 to 1, default 1.0) says how directly the video serves the user's goal:
1.0 directly on topic, 0.5 partly (useful side material), 0.2 off topic.
"segments" on a video (optional) lists the time ranges that hold its key points:
  "segments": [{"start_s": 0, "end_s": 1331, "points": ["K1", "K2"], "relevance": 1.0}]
A segment's optional "relevance" (0 to 1) multiplies the video's, for a stretch that is
off the goal even though the video as a whole is on it.
When present, the watch plan is built from segments, so it can say "watch 0:00 to
22:11" instead of the whole video. Without it a video is one segment.

The scoring is deterministic: the same input always gives the same output.

Input is VALIDATED first. A mistyped point id used to be dropped silently (the video
just lost a point and still got a plausible score). Now it is an error: the script
prints what is wrong to stderr and exits 2. Softer problems (a video with no points,
a point nobody references) go into the output's "warnings" list.
"""
import json
import sys
from collections import Counter

SCORER_VERSION = "3"   # 3: segment-aware watch plan, per-minute stop rule (minutes floored at 3); scores unchanged

POINT_VALUE = {
    "SUPPORTED": 1.0,
    "PARTLY": 0.6,
    "OUTDATED": 0.3,
    "UNVERIFIED": 0.7,
    "NOT_CHECKED": 0.7,
    "CONTRADICTED": 0.0,
}
VALIDITY_CREDIT = {"SUPPORTED": 1.0, "PARTLY": 0.6, "OUTDATED": 0.3, "CONTRADICTED": 0.0}
WEIGHTS = {"validity": 0.35, "coverage": 0.25, "density": 0.25, "signal": 0.15}
CONTRADICTION_PENALTY = 12
RELEVANCE_FLOOR = 0.4  # an off-topic video keeps at least this share of its score
STOP_BELOW = 0.10  # stop the watch plan when a whole video adds under 10% of total value
SEGMENT_STOP_BELOW = 0.04  # ...and a single segment under 4% (segments are smaller by nature)
MIN_RATE_FRACTION = 0.20   # ...or when it adds value at under 20% of the best pick's per-minute rate
RATE_MIN_MINUTES = 3.0     # a shorter segment is compared as if it were this long (see rate_of)
# 0.20 was set from one real run (it dropped a 6-minute segment worth 5%). A second run showed
# a 1.7-minute opening setting a rate so high that a 7-minute core tutorial section fell under
# the cutoff; flooring the minutes in the comparison fixes that. Tune both with more runs.


def point_value(p):
    base = POINT_VALUE.get(p.get("verdict", "NOT_CHECKED"), 0.7)
    return base * (2.0 if p.get("core") else 1.0)


def validate(data):
    """Return (errors, warnings). Errors make the run fail; warnings are reported."""
    errors, warnings = [], []
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list) \
            or not isinstance(data.get("points"), list):
        return ['input must be an object with "videos" and "points" lists'], warnings

    pids = [p.get("id") for p in data["points"] if isinstance(p, dict)]
    if len(pids) != len(data["points"]) or any(i is None for i in pids):
        errors.append("every point needs an id")
    for pid, n in Counter(pids).items():
        if n > 1:
            errors.append(f"duplicate point id {pid!r}")
    pset = set(pids)
    for p in data["points"]:
        verdict = p.get("verdict", "NOT_CHECKED")
        if verdict not in POINT_VALUE:
            errors.append(f"point {p.get('id')!r} has unknown verdict {verdict!r}")

    vids = [v.get("id") for v in data["videos"] if isinstance(v, dict)]
    if len(vids) != len(data["videos"]) or any(i is None for i in vids):
        errors.append("every video needs an id")
    for vid, n in Counter(vids).items():
        if n > 1:
            errors.append(f"duplicate video id {vid!r}")

    referenced = set()
    for v in data["videos"]:
        if not isinstance(v, dict):
            continue
        vid = v.get("id")
        pts = v.get("points", [])
        unknown = [i for i in pts if i not in pset]
        if unknown:
            errors.append(f"video {vid!r} references unknown point ids: {unknown}")
        referenced.update(i for i in pts if i in pset)
        if not pts:
            warnings.append(f"video {vid!r} has no key points, so it will score near zero")
        rel = v.get("relevance", 1.0)
        try:
            if not 0.0 <= float(rel) <= 1.0:
                errors.append(f"video {vid!r} relevance {rel} is outside 0 to 1")
        except (TypeError, ValueError):
            errors.append(f"video {vid!r} relevance {rel!r} is not a number")
        if not v.get("duration_s"):
            warnings.append(f"video {vid!r} has no duration_s; it is treated as 30 seconds")
        for k in ("vague", "pitches"):
            if v.get(k, 0) < 0:
                errors.append(f"video {vid!r} has negative {k}")
        segs = v.get("segments")
        if segs is not None and not isinstance(segs, list):
            errors.append(f"video {vid!r} segments must be a list")
            segs = []
        for s in segs or []:
            if not isinstance(s, dict):
                errors.append(f"video {vid!r} has a segment that is not an object")
                continue
            a, b = s.get("start_s"), s.get("end_s")
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)) \
                    or a < 0 or b <= a:
                errors.append(f"video {vid!r} has a segment with a bad range {a!r} to {b!r}")
            elif v.get("duration_s") and b > v["duration_s"] + 5:
                warnings.append(f"video {vid!r} segment {a}-{b}s ends after the video does")
            srel = s.get("relevance", 1.0)
            if not isinstance(srel, (int, float)) or isinstance(srel, bool) or not 0.0 <= srel <= 1.0:
                errors.append(f"video {vid!r} segment relevance {srel!r} is outside 0 to 1")
            bad = [i for i in s.get("points", []) if i not in pset]
            if bad:
                errors.append(f"video {vid!r} segment references unknown point ids: {bad}")

    orphans = sorted(pset - referenced)
    if orphans:
        warnings.append(f"{len(orphans)} point(s) are not referenced by any video: {orphans[:8]}")
    return errors, warnings


def score(data, warnings):
    points = {p["id"]: p for p in data["points"]}
    vids = data["videos"]
    total_value = sum(point_value(p) for p in points.values()) or 1.0

    rows = []
    for v in vids:
        ids = [i for i in dict.fromkeys(v["points"]) if i in points]
        value = sum(point_value(points[i]) for i in ids)
        minutes = max(v.get("duration_s", 0) / 60.0, 0.5)
        verdicts = [points[i].get("verdict", "NOT_CHECKED") for i in ids]
        checked = [x for x in verdicts if x in VALIDITY_CREDIT]
        credit = sum(VALIDITY_CREDIT[x] for x in checked)
        validity = (credit + 0.7) / (len(checked) + 1)
        n_contra = sum(1 for i in ids if points[i].get("verdict") == "CONTRADICTED"
                       and points[i].get("core"))
        n_pts = len(ids)
        signal = n_pts / (n_pts + v.get("vague", 0) + 2 * v.get("pitches", 0)) if n_pts else 0.0
        rows.append({
            "id": v["id"], "title": v.get("title", ""), "minutes": round(minutes, 1),
            "points": n_pts, "checked": len(checked),
            "contradicted_core": n_contra,
            "value": value, "coverage": value / total_value,
            "density": value / (minutes / 10.0),
            "validity": validity, "signal": signal,
            "relevance": min(1.0, max(0.0, float(v.get("relevance", 1.0)))),
            "confidence": "low" if len(checked) < 2 else ("medium" if len(checked) < 4 else "high"),
            "_ids": set(ids),
        })

    max_cov = max((r["coverage"] for r in rows), default=1.0) or 1.0
    max_den = max((r["density"] for r in rows), default=1.0) or 1.0
    for r in rows:
        parts = {
            "validity": r["validity"],
            "coverage": r["coverage"] / max_cov,
            "density": r["density"] / max_den,
            "signal": r["signal"],
        }
        raw = 100 * sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)
        factor = RELEVANCE_FLOOR + (1 - RELEVANCE_FLOOR) * r["relevance"]
        r["relevance_factor"] = round(factor, 2)
        r["score"] = round(max(0.0, raw * factor - CONTRADICTION_PENALTY * r["contradicted_core"]), 1)
        r["parts"] = {k: round(val, 2) for k, val in parts.items()}

    ranked = sorted(rows, key=lambda r: r["score"], reverse=True)

    plan, watch_ranges, skipped = build_watch_plan(vids, rows, points, total_value)

    for r in ranked:
        r.pop("_ids")
        for k in ("value", "coverage", "density", "validity", "signal"):
            r[k] = round(r[k], 2)
    return {"scorer_version": SCORER_VERSION, "warnings": warnings, "ranked": ranked,
            "watch_plan": plan, "watch_ranges": watch_ranges, "skip_for_new_info": skipped}


def build_watch_plan(vids, rows, points, total_value):
    """Greedy by relevance-weighted new value per minute.

    The unit is a SEGMENT when a video supplies "segments" (time ranges that hold its
    key points, from rank_tools.py build-input), otherwise the whole video. A long
    video whose useful part is its first half is then planned as that half.
    Stops when the next pick adds too little in total, or adds value far more slowly
    than the best pick did (so a long video with one useful minute is not planned
    just because its total share clears the bar).
    """
    by_id = {r["id"]: r for r in rows}
    cands = []
    for v in vids:
        r = by_id[v["id"]]
        segs = v.get("segments") or []
        if segs:
            for s in segs:
                ids = {i for i in s.get("points", []) if i in points}
                seg_rel = min(1.0, max(0.0, float(s.get("relevance", 1.0))))
                cands.append({"id": v["id"], "start_s": int(s["start_s"]), "end_s": int(s["end_s"]),
                              "ids": ids, "segment": True, "relevance": r["relevance"] * seg_rel,
                              "minutes": max((s["end_s"] - s["start_s"]) / 60.0, 0.5)})
        else:
            cands.append({"id": v["id"], "start_s": 0, "end_s": int(v.get("duration_s", 0) or 0),
                          "ids": r["_ids"], "segment": False, "relevance": r["relevance"],
                          "minutes": r["minutes"]})

    seen, plan, cum_min, first_rate = set(), [], 0.0, None
    remaining = list(cands)

    def gain(c):
        # new value, weighted by how relevant the video is to the goal
        return c["relevance"] * sum(point_value(points[i]) for i in c["ids"] - seen)

    def rate_of(c):
        # Floor the length: a 1.7-minute opening with a few points would otherwise set a rate
        # so high that every real tutorial section looks slow beside it.
        return gain(c) / total_value / max(c["minutes"], RATE_MIN_MINUTES)

    while remaining:
        # Only candidates that add enough, and add it fast enough, are eligible. Filtering
        # first (rather than testing the single best pick) stops a tiny high-rate segment
        # from ending the plan while a bigger useful one is still on the table.
        eligible = [c for c in remaining
                    if gain(c) / total_value >= (SEGMENT_STOP_BELOW if c["segment"] else STOP_BELOW)
                    and rate_of(c) >= MIN_RATE_FRACTION * (first_rate or 0.0)]
        if not eligible:
            if plan:
                break
            eligible = remaining          # the plan always has a first pick
        best = max(eligible, key=rate_of)
        g = gain(best)
        rate = rate_of(best)
        if first_rate is None:
            first_rate = rate
        added = sorted(best["ids"] - seen, key=lambda i: (len(i), i))
        seen |= best["ids"]
        cum_min += best["minutes"]
        plan.append({
            "id": best["id"], "start_s": best["start_s"], "end_s": best["end_s"],
            "minutes": round(best["minutes"], 1), "points_added": added,
            "new_value_share": round(g / total_value, 2),
            "cumulative_coverage": round(
                sum(point_value(points[i]) for i in seen) / total_value, 2),
            "cumulative_minutes": round(cum_min, 1),
        })
        remaining.remove(best)

    # One entry per video, ranges in time order, touching ranges joined.
    order, rng = [], {}
    for p in plan:
        if p["id"] not in rng:
            order.append(p["id"])
            rng[p["id"]] = []
        rng[p["id"]].append([p["start_s"], p["end_s"]])
    watch_ranges = []
    for vid in order:
        merged = []
        for s, e in sorted(rng[vid]):
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        watch_ranges.append({"id": vid, "ranges": merged,
                             "minutes": round(sum(e - s for s, e in merged) / 60.0, 1)})
    skipped = [v["id"] for v in vids if v["id"] not in rng]
    return plan, watch_ranges, skipped


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"rank_videos: input is not valid JSON: {e}\n")
        return 2
    errors, warnings = validate(data)
    if errors:
        sys.stderr.write("rank_videos: invalid input, nothing scored:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        return 2
    for w in warnings:
        sys.stderr.write(f"rank_videos: warning: {w}\n")
    json.dump(score(data, warnings), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
