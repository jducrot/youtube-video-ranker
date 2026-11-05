"""The scoring script embedded in SKILL.md is deterministic, so test it as such."""
from __future__ import annotations

import copy

import pytest

from conftest import make_points


def _video(vid, points, minutes=10, vague=0, pitches=0, **extra):
    return {"id": vid, "title": vid, "duration_s": minutes * 60,
            "vague": vague, "pitches": pitches, "points": points, **extra}


PTS = make_points(("K1", True, "SUPPORTED"), ("K2", True, "SUPPORTED"),
                  ("K3", False, "NOT_CHECKED"))


def test_runs_and_is_deterministic(run_scorer):
    data = {"videos": [_video("a", ["K1", "K2"]), _video("b", ["K3"])],
            "points": PTS}
    assert run_scorer(data) == run_scorer(copy.deepcopy(data))


def test_relevance_defaults_to_one(run_scorer):
    base = {"videos": [_video("a", ["K1", "K2"]), _video("b", ["K3"])], "points": PTS}
    explicit = copy.deepcopy(base)
    for v in explicit["videos"]:
        v["relevance"] = 1.0
    a, b = run_scorer(base), run_scorer(explicit)
    assert [r["score"] for r in a["ranked"]] == [r["score"] for r in b["ranked"]]
    assert a["watch_plan"] == b["watch_plan"]


def test_off_topic_video_is_demoted_by_the_documented_factor(run_scorer):
    # identical videos, so the only difference is relevance
    data = {"videos": [_video("on", ["K1", "K2"], relevance=1.0),
                       _video("off", ["K1", "K2"], relevance=0.2)], "points": PTS}
    r = {x["id"]: x for x in run_scorer(data)["ranked"]}
    assert r["on"]["score"] > r["off"]["score"]
    assert r["off"]["relevance_factor"] == 0.52          # 0.4 + 0.6 * 0.2
    assert abs(r["off"]["score"] - r["on"]["score"] * 0.52) <= 0.1


def test_uniform_relevance_does_not_change_the_order(run_scorer):
    base = {"videos": [_video("a", ["K1", "K2"]), _video("b", ["K3"], minutes=5)],
            "points": PTS}
    scaled = copy.deepcopy(base)
    for v in scaled["videos"]:
        v["relevance"] = 0.5
    assert ([r["id"] for r in run_scorer(base)["ranked"]]
            == [r["id"] for r in run_scorer(scaled)["ranked"]])


def test_contradicted_core_point_costs_at_least_twelve_points(run_scorer):
    ok = {"videos": [_video("a", ["K1"])], "points": make_points(("K1", True, "SUPPORTED"))}
    bad = {"videos": [_video("a", ["K1"])], "points": make_points(("K1", True, "CONTRADICTED"))}
    s_ok = run_scorer(ok)["ranked"][0]
    s_bad = run_scorer(bad)["ranked"][0]
    assert s_bad["contradicted_core"] == 1
    assert s_ok["score"] - s_bad["score"] >= 12


def test_contradicted_non_core_point_has_no_penalty_flag(run_scorer):
    d = {"videos": [_video("a", ["K1"])], "points": make_points(("K1", False, "CONTRADICTED"))}
    assert run_scorer(d)["ranked"][0]["contradicted_core"] == 0


def test_unverified_does_not_count_against_validity_as_a_failure(run_scorer):
    sup = {"videos": [_video("a", ["K1"])], "points": make_points(("K1", True, "SUPPORTED"))}
    unv = {"videos": [_video("a", ["K1"])], "points": make_points(("K1", True, "UNVERIFIED"))}
    # UNVERIFIED is neutral: it is not scored as a failed check
    assert run_scorer(unv)["ranked"][0]["checked"] == 0
    assert run_scorer(sup)["ranked"][0]["checked"] == 1


def test_confidence_levels_follow_the_number_of_checked_points(run_scorer):
    pts = make_points(*[(f"K{i}", True, "SUPPORTED") for i in range(5)])
    for n, expected in [(1, "low"), (2, "medium"), (3, "medium"), (4, "high")]:
        d = {"videos": [_video("a", [f"K{i}" for i in range(n)])], "points": pts}
        assert run_scorer(d)["ranked"][0]["confidence"] == expected


def test_watch_plan_stops_when_a_video_adds_almost_nothing(run_scorer):
    data = {"videos": [_video("a", ["K1", "K2"]), _video("b", ["K1", "K2"])], "points": PTS[:2]}
    out = run_scorer(data)
    assert len(out["watch_plan"]) == 1
    assert len(out["skip_for_new_info"]) == 1


def test_watch_plan_prefers_relevant_video_over_bigger_off_topic_one(run_scorer):
    pts = make_points(*[(f"K{i}", True, "SUPPORTED") for i in range(8)])
    data = {"videos": [
        _video("big_off_topic", [f"K{i}" for i in range(6)], relevance=0.2),
        _video("small_on_topic", ["K6", "K7"], relevance=1.0)], "points": pts}
    assert run_scorer(data)["watch_plan"][0]["id"] == "small_on_topic"


def test_shorter_video_with_same_points_scores_higher_on_density(run_scorer):
    data = {"videos": [_video("short", ["K1", "K2"], minutes=5),
                       _video("long", ["K1", "K2"], minutes=50)], "points": PTS[:2]}
    ranked = run_scorer(data)["ranked"]
    assert ranked[0]["id"] == "short"


# ------------------------------------------------------------------ validation
# A mistyped point id used to be dropped silently: the video just lost a point and
# still got a plausible score. These tests pin the new behaviour.

def test_unknown_point_id_is_an_error_not_a_silent_drop(run_scorer_raw):
    d = {"videos": [_video("a", ["K1", "K2", "K33"])], "points": PTS}
    r = run_scorer_raw(d)
    assert r.returncode == 2 and r.stdout == ""
    assert "unknown point ids" in r.stderr and "K33" in r.stderr


def test_duplicate_ids_are_errors(run_scorer_raw):
    dup_point = {"videos": [_video("a", ["K1"])], "points": PTS + make_points(("K1", True, "SUPPORTED"))}
    dup_video = {"videos": [_video("a", ["K1"]), _video("a", ["K2"])], "points": PTS}
    assert "duplicate point id" in run_scorer_raw(dup_point).stderr
    assert "duplicate video id" in run_scorer_raw(dup_video).stderr


def test_bad_verdict_and_bad_relevance_are_errors(run_scorer_raw):
    bad_verdict = {"videos": [_video("a", ["K1"])], "points": make_points(("K1", True, "KINDA_TRUE"))}
    bad_rel = {"videos": [_video("a", ["K1"], relevance=1.5)], "points": PTS[:1]}
    assert "unknown verdict" in run_scorer_raw(bad_verdict).stderr
    assert "outside 0 to 1" in run_scorer_raw(bad_rel).stderr


def test_malformed_input_exits_2_with_a_message(run_scorer_raw):
    assert run_scorer_raw("not json").returncode == 2
    assert "videos" in run_scorer_raw({"nope": 1}).stderr


def test_soft_problems_are_warnings_in_the_output(run_scorer):
    d = {"videos": [_video("a", ["K1"]), _video("empty", [])], "points": PTS}   # K2, K3 unused
    out = run_scorer(d)
    joined = " ".join(out["warnings"])
    assert "no key points" in joined and "not referenced" in joined
    assert out["scorer_version"] == "3"


# ------------------------------------------------------------ segment watch plan
def _seg(a, b, points, **extra):
    return {"start_s": a, "end_s": b, "points": points, **extra}


SEG_PTS = make_points(*[(f"K{i}", True, "SUPPORTED") for i in range(1, 9)])


def test_a_long_video_is_planned_by_segment_not_whole(run_scorer):
    long = _video("long", [f"K{i}" for i in range(1, 7)], minutes=40,
                  segments=[_seg(0, 600, ["K1", "K2", "K3", "K4", "K5"]), _seg(600, 2400, ["K6"])])
    short = _video("short", ["K7", "K8"], minutes=4)
    out = run_scorer({"videos": [long, short], "points": SEG_PTS})
    steps = [(p["id"], p["start_s"], p["end_s"]) for p in out["watch_plan"]]
    assert ("long", 0, 600) in steps and ("long", 600, 2400) not in steps
    assert {w["id"]: w["ranges"] for w in out["watch_ranges"]}["long"] == [[0, 600]]


def test_without_segments_a_video_is_one_whole_step(run_scorer):
    out = run_scorer({"videos": [_video("a", ["K1", "K2"], minutes=10)], "points": SEG_PTS[:2]})
    (step,) = out["watch_plan"]
    assert (step["start_s"], step["end_s"], step["minutes"]) == (0, 600, 10.0)
    assert step["points_added"] == ["K1", "K2"]


def test_an_off_goal_segment_is_dropped_from_the_plan(run_scorer):
    segs = [_seg(0, 600, ["K1", "K2", "K3"]), _seg(600, 1200, ["K4", "K5", "K6"], relevance=0.1)]
    pts = make_points(*[(f"K{i}", True, "SUPPORTED") for i in range(1, 7)])
    out = run_scorer({"videos": [_video("v", [f"K{i}" for i in range(1, 7)], minutes=20,
                                        segments=segs)], "points": pts})
    assert [(p["start_s"], p["end_s"]) for p in out["watch_plan"]] == [(0, 600)]


def test_a_tiny_fast_segment_does_not_end_the_plan_early(run_scorer):
    # A 30 s segment with one small new point has the best rate but is below the share
    # floor; the larger useful segment must still be planned.
    pts = make_points(*([("K1", False, "SUPPORTED")] + [(f"K{i}", True, "SUPPORTED") for i in range(2, 10)]))
    segs = [_seg(0, 30, ["K1"]), _seg(30, 900, [f"K{i}" for i in range(2, 10)])]
    out = run_scorer({"videos": [_video("v", [f"K{i}" for i in range(1, 10)], minutes=15,
                                        segments=segs)], "points": pts})
    assert (30, 900) in [(p["start_s"], p["end_s"]) for p in out["watch_plan"]]


def test_a_slow_segment_is_skipped_even_when_its_share_is_large(run_scorer):
    pts = make_points(*[(f"K{i}", True, "SUPPORTED") for i in range(1, 9)])
    fast = _video("fast", ["K1", "K2", "K3", "K4"], minutes=2)
    slow = _video("slow", ["K5", "K6"], minutes=120)          # 12% of the value, at a crawl
    out = run_scorer({"videos": [fast, slow, _video("other", ["K7", "K8"], minutes=3)], "points": pts})
    assert "slow" in out["skip_for_new_info"]


@pytest.mark.parametrize("seg,needle", [
    (_seg(100, 50, ["K1"]), "bad range"),
    (_seg(0, 50, ["K99"]), "unknown point ids"),
    (_seg(0, 50, ["K1"], relevance=1.5), "outside 0 to 1"),
])
def test_bad_segments_are_errors(run_scorer_raw, seg, needle):
    r = run_scorer_raw({"videos": [_video("a", ["K1"], segments=[seg])], "points": PTS})
    assert r.returncode == 2 and needle in r.stderr


def test_a_segment_past_the_end_is_only_a_warning(run_scorer):
    out = run_scorer({"videos": [_video("a", ["K1"], minutes=1, segments=[_seg(0, 600, ["K1"])])],
                      "points": PTS})
    assert any("ends after the video" in w for w in out["warnings"])


def test_a_short_dense_opening_does_not_hide_a_core_tutorial_section(run_scorer):
    # Found on a real run: a 1.7-minute opening with two core points had such a high rate that
    # the 7-minute section holding the tutorial itself fell under 20% of it and was dropped.
    pts = make_points(("K1", True, "SUPPORTED"), ("K2", True, "SUPPORTED"),
                      ("K3", True, "SUPPORTED"), ("K4", False, "NOT_CHECKED"),
                      *[(f"K{i}", False, "SUPPORTED") for i in range(5, 10)])
    guide = _video("guide", ["K1", "K2", "K3", "K4"], minutes=10,
                   segments=[_seg(0, 100, ["K1", "K2"]), _seg(100, 520, ["K3", "K4"])])
    filler = _video("filler", [f"K{i}" for i in range(5, 10)], minutes=60)
    out = run_scorer({"videos": [guide, filler], "points": pts})
    steps = [(p["id"], p["start_s"], p["end_s"]) for p in out["watch_plan"]]
    assert ("guide", 0, 100) in steps and ("guide", 100, 520) in steps
    assert "filler" in out["skip_for_new_info"]
