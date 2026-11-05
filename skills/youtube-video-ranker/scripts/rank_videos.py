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

The scoring is deterministic: the same input always gives the same output.

Input is VALIDATED first. A mistyped point id used to be dropped silently (the video
just lost a point and still got a plausible score). Now it is an error: the script
prints what is wrong to stderr and exits 2. Softer problems (a video with no points,
a point nobody references) go into the output's "warnings" list.
"""
import json
import sys
from collections import Counter

SCORER_VERSION = "2"

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
STOP_BELOW = 0.10  # stop the watch plan when a video adds under 10% of total value


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

    # Watch plan: greedy by new value per minute, stop when little is left to learn.
    seen, plan, cum_min, remaining = set(), [], 0.0, list(rows)
    while remaining:
        def gain(r):
            # new value, weighted by how relevant the video is to the goal
            return r["relevance"] * sum(point_value(points[i]) for i in r["_ids"] - seen)
        best = max(remaining, key=lambda r: gain(r) / r["minutes"])
        g = gain(best)
        if plan and g / total_value < STOP_BELOW:
            break
        seen |= best["_ids"]
        cum_min += best["minutes"]
        plan.append({
            "id": best["id"], "new_value_share": round(g / total_value, 2),
            "cumulative_coverage": round(
                sum(point_value(points[i]) for i in seen) / total_value, 2),
            "cumulative_minutes": round(cum_min, 1),
        })
        remaining.remove(best)
    skipped = [r["id"] for r in remaining]

    for r in ranked:
        r.pop("_ids")
        for k in ("value", "coverage", "density", "validity", "signal"):
            r[k] = round(r[k], 2)
    return {"scorer_version": SCORER_VERSION, "warnings": warnings, "ranked": ranked,
            "watch_plan": plan, "skip_for_new_info": skipped}


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
