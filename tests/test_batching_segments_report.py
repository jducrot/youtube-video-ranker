"""4.1: no-transcript scan result, inventory batching, segment-aware planning inputs, and the
report checker. Shares the `world` fixture with test_rank_tools."""
from __future__ import annotations

import json

import pytest

from conftest import SCRIPTS
from test_rank_tools import A, B, run_tool, world  # noqa: F401  (world is a fixture)


# ------------------------------------------------- injection scan: no transcript
def test_scan_says_so_when_a_video_has_no_transcript(tmp_path):
    (tmp_path / f"{A}.json").write_text(json.dumps(
        {"error": "NO_TRANSCRIPT", "message": "No captions available", "video_id": A}), encoding="utf-8")
    (tmp_path / f"{B}.json").write_text(json.dumps({"transcript": []}), encoding="utf-8")
    out = json.loads(run_tool("scan-injection", A, B, "--cache-dir", tmp_path).stdout)
    for vid in (A, B):
        assert out[vid]["scanned"] is False and "hits" not in out[vid]
    assert "NO_TRANSCRIPT" in out[A]["error"]


# ------------------------------------------------------------ inventory batching
def _meta(minutes, words):
    return {"duration_s": minutes * 60, "words": words}


def test_short_videos_share_an_agent_and_long_ones_stay_solo(rank_tools):
    vids = {"long": _meta(30, 5000), "s1": _meta(2, 300), "s2": _meta(3, 400), "s3": _meta(4, 600),
            "s4": _meta(5, 700)}
    batches = rank_tools.plan_batches(vids, short_s=600, max_batch=3, max_total_s=24 * 60, max_words=6000)
    assert batches[0] == ["long"]
    assert all(len(b) <= 3 for b in batches)
    assert sorted(v for b in batches for v in b) == sorted(vids)       # every video exactly once
    assert len(batches) == 3                                            # the long one + two batches


def test_batching_respects_the_minute_and_word_caps(rank_tools):
    vids = {f"s{i}": _meta(9, 1000) for i in range(4)}
    by_minutes = rank_tools.plan_batches(vids, 600, 3, 20 * 60, 6000)   # 9 + 9 fits, 27 does not
    assert sorted(len(b) for b in by_minutes) == [2, 2]
    by_words = rank_tools.plan_batches(vids, 600, 3, 99 * 60, 1500)
    assert all(len(b) == 1 for b in by_words)


def test_max_batch_one_turns_batching_off(rank_tools):
    vids = {f"s{i}": _meta(2, 100) for i in range(5)}
    out = rank_tools.plan_batches(vids, 600, 1, 24 * 60, 6000)
    assert sorted(out) == [[v] for v in sorted(vids)]


def test_plan_inventory_cli_reports_agents_and_skips_uncached(tmp_path):
    for vid, minutes in ((A, 3), (B, 4)):
        (tmp_path / f"{vid}.json").write_text(json.dumps(
            {"video_id": vid, "duration_seconds": minutes * 60,
             "transcript": [{"text": "word " * 100, "start": 0, "duration": 1}]}), encoding="utf-8")
    out = json.loads(run_tool("plan-inventory", A, B, "CCCCCCCCCCC", "--cache-dir", tmp_path).stdout)
    assert out["agents"] == 1 and out["agents_without_batching"] == 2
    assert sorted(out["batches"][0]) == [A, B] and out["skipped"] == {"CCCCCCCCCCC": "not cached"}
    off = json.loads(run_tool("plan-inventory", A, B, "--cache-dir", tmp_path, "--max-batch", 1).stdout)
    assert off["agents"] == 2


# --------------------------------------------------------------------- segments
SECTIONS = [{"start_s": 0, "end_s": 600, "type": "content"}, {"start_s": 600, "end_s": 1200, "type": "content"},
            {"start_s": 1200, "end_s": 1230, "type": "outro"}]


def test_references_are_grouped_into_the_sections_that_hold_them(rank_tools):
    segs = rank_tools.build_segments(SECTIONS, [("K1", 30), ("K2", 100), ("K3", 700)])
    assert segs == [{"start_s": 0, "end_s": 600, "points": ["K1", "K2"]},
                    {"start_s": 600, "end_s": 1200, "points": ["K3"]}]


def test_a_reference_after_the_last_section_extends_it(rank_tools):
    segs = rank_tools.build_segments(SECTIONS[:2], [("K1", 700), ("K2", 1300)])
    assert segs == [{"start_s": 600, "end_s": 1320, "points": ["K1", "K2"]}]


def test_tiny_sections_are_joined_to_their_nearest_neighbour(rank_tools):
    sections = [{"start_s": 0, "end_s": 20, "type": "hook"}, {"start_s": 20, "end_s": 600, "type": "content"}]
    segs = rank_tools.build_segments(sections, [("K1", 5), ("K2", 100)])
    assert segs == [{"start_s": 0, "end_s": 600, "points": ["K1", "K2"]}]


def test_no_sections_means_no_segments(rank_tools):
    assert rank_tools.build_segments([], [("K1", 5)]) == []


def test_ranges_down_weight_the_segments_they_mostly_cover(rank_tools):
    errors = []
    segs = [{"start_s": 0, "end_s": 600, "points": ["K1"]}, {"start_s": 600, "end_s": 1200, "points": ["K2"]}]
    out = rank_tools.apply_ranges(segs, [{"start": "10:00", "end": "20:00", "relevance": 0.2}], "v", errors)
    assert out[0] == segs[0] and out[1]["relevance"] == 0.2 and not errors


def test_bad_ranges_are_reported(rank_tools):
    errors = []
    rank_tools.apply_ranges([], [{"start": "20:00", "end": "10:00", "relevance": 0.2},
                                 {"start": "1:00", "end": "2:00", "relevance": 3}], "v", errors)
    assert len(errors) == 2


def test_build_input_adds_segments_ranges_and_facts(world):  # noqa: F811
    sections = [{"id": "S1", "type": "content", "start": "0:00", "end": "1:00", "summary": ""},
                {"id": "S2", "type": "content", "start": "1:00", "end": "10:00", "summary": ""}]
    (world.cache / f"{A}-pass1.json").write_text(json.dumps({"output": {"sections": sections}}), encoding="utf-8")
    kp = json.loads(json.dumps(world.kp))
    kp["videos"][A]["ranges"] = [{"start": "1:00", "end": "10:00", "relevance": 0.3, "reason": "off topic"}]
    world.write(kp)
    out = json.loads(world.build("--score").stdout)
    ri = json.loads((world.analysis / "rank_input.json").read_text(encoding="utf-8"))
    a = next(v for v in ri["videos"] if v["id"] == A)
    assert a["segments"] and any(s.get("relevance") == 0.3 for s in a["segments"])
    assert out["facts"][A]["inventory_items"] == 7          # 2 concrete + 1 evidence + 3 vague + 1 pitch
    assert out["facts"][A]["vague_or_pitch_pct"] == round(100 * 4 / 7, 1)
    assert "segments" not in next(v for v in ri["videos"] if v["id"] == B)   # no pass1 for B


# ----------------------------------------------------------------- check-report
def _good_report(world):  # noqa: F811
    ro = json.loads((world.analysis / "rank_output.json").read_text(encoding="utf-8"))
    ri = json.loads((world.analysis / "rank_input.json").read_text(encoding="utf-8"))
    version = (SCRIPTS / "VERSION").read_text().strip()
    verdict = {p["id"]: p["verdict"] for p in ri["points"]}
    rows = []
    for pos, r in enumerate(ro["ranked"], start=1):
        v = next(x for x in ri["videos"] if x["id"] == r["id"])
        d = v["duration_s"]
        tally = [sum(1 for i in v["points"] if verdict[i] == w) for w in ("SUPPORTED", "PARTLY", "CONTRADICTED")]
        rows.append(f"| {pos} | [{v['title']}](https://youtu.be/{r['id']}) | {d // 60}:{d % 60:02d} | "
                    f"{r['score']:.1f} | {r['relevance']:.1f} | {r['confidence']} | {r['points']} | "
                    f"{tally[0]} / {tally[1]} / {tally[2]} | {v['vague']} | {v['pitches']} |")
    plan = ro["watch_ranges"][0]
    return "\n".join([
        "# Report", f"Pipeline version {version}.", "## 1. Recommendation", "Watch A.",
        "## 2. Ranking",
        "| Rank | Video | Length | Score | Relevance | Confidence | Key points | Checked | Vague | Pitches |",
        "|---|---|---|---|---|---|---|---|---|---|", *rows, "",
        "## 3. Watch plan", f"- [0:30](https://youtu.be/{plan['id']}?t=30)",
        "## 4. Conflicts and contradictions",
        "| Claim | Verdict | Source |", "|---|---|---|",
        "| claim one | SUPPORTED | [x](https://example.com) |", "",
        "## 5. Per-video notes", "Notes.", "## 6. Left out", "None.",
        "## 7. Limits", "2 of 3 key points were checked."])


def _write(world, text):  # noqa: F811
    p = world.analysis / "report.md"
    p.write_text(text, encoding="utf-8")
    return p


def _check(world, text):  # noqa: F811
    return run_tool("check-report", "--analysis", world.analysis, "--report", _write(world, text),
                    "--cache-dir", world.cache)


def test_check_report_accepts_a_report_that_matches_the_data(world):  # noqa: F811
    world.build("--score")
    r = _check(world, _good_report(world))
    assert r.returncode == 0, r.stdout
    assert json.loads(r.stdout)["ok"]


@pytest.mark.parametrize("mutate,needle", [
    (lambda t: t.replace((SCRIPTS / "VERSION").read_text().strip(), "9.9.9"), "pipeline version"),
    (lambda t: t.replace("## 7. Limits", "## 7. Method"), "no 'limits' section"),
    (lambda t: t.replace("2 of 3 key points", "3 of 3 key points"), "'3 of 3 key points'"),
    (lambda t: t.replace("[x](https://example.com)", "[x](https://made-up.example/source)"), "not a source"),
    (lambda t: t.replace("| SUPPORTED |", "| CONTRADICTED |"), "CONTRADICTED verdicts"),
    (lambda t: t + "\n[go](https://youtu.be/ZZZZZZZZZZZ?t=5)", "not a ranked video"),
    (lambda t: t + f"\n[go](https://youtu.be/{A}?t=99999)", "past the end"),
])
def test_check_report_catches_these_mistakes(world, mutate, needle):  # noqa: F811
    world.build("--score")
    r = _check(world, mutate(_good_report(world)))
    assert r.returncode == 2
    assert needle in " ".join(json.loads(r.stdout)["errors"])


def test_check_report_catches_a_wrong_score_count_length_and_order(world):  # noqa: F811
    world.build("--score")
    ro = json.loads((world.analysis / "rank_output.json").read_text(encoding="utf-8"))
    top, second = ro["ranked"][0], ro["ranked"][1]
    base = _good_report(world)

    wrong_score = base.replace(f"| {top['score']:.1f} |", f"| {top['score'] + 1:.1f} |", 1)
    r = _check(world, wrong_score)
    assert r.returncode == 2 and "does not show score" in r.stdout

    lines = base.splitlines()
    i = next(k for k, ln in enumerate(lines) if f"youtu.be/{top['id']})" in ln)
    row = lines[i].replace("| 1 |", "| 1 |")
    lines[i] = row.replace(" | 10:00 | ", " | 12:00 | ").replace(" | 20:00 | ", " | 25:00 | ")
    r = _check(world, "\n".join(lines))
    assert r.returncode == 2 and "table length" in r.stdout

    lines = base.splitlines()
    j = next(k for k, ln in enumerate(lines) if f"youtu.be/{second['id']})" in ln)
    i = next(k for k, ln in enumerate(lines) if f"youtu.be/{top['id']})" in ln)
    lines[i], lines[j] = lines[j], lines[i]
    r = _check(world, "\n".join(lines))
    assert r.returncode == 2 and "sits in row" in r.stdout

    lines = base.splitlines()
    i = next(k for k, ln in enumerate(lines) if f"youtu.be/{top['id']})" in ln)
    lines[i] = lines[i].replace(" / 0 |", " / 1 |")
    r = _check(world, "\n".join(lines))
    assert r.returncode == 2 and "checked tally" in r.stdout
