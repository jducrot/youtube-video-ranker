"""4.2: write-pass (the cache written from the pass output alone), draft-report (the
report's numbers written by code), the standalone single-video mode, and the fact-check
lookup count. Shares the `world` fixture with test_rank_tools."""
from __future__ import annotations

import json
import re

import pytest

from conftest import SCRIPTS
from test_rank_tools import A, B, run_tool, world  # noqa: F401  (world is a fixture)

# A stand-in for youtube-claims' cache.py: same CLI, same wrapper shape, same hashing idea.
STUB_CACHE_PY = r'''
import argparse, hashlib, json, sys
from pathlib import Path
ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd")
w = sub.add_parser("write"); w.add_argument("pass_n"); w.add_argument("video_id"); w.add_argument("prompt_path")
w.add_argument("--cache-dir", required=True)
v = sub.add_parser("verify-quotes"); v.add_argument("video_id"); v.add_argument("--cache-dir", required=True)
a = ap.parse_args(); cache = Path(a.cache_dir)
if a.cmd == "write":
    p = json.load(sys.stdin)
    h = hashlib.sha256(json.dumps(p["inputs"], sort_keys=True).encode()).hexdigest()
    (cache / f"{a.video_id}-pass{a.pass_n}.json").write_text(json.dumps(
        {"video_id": a.video_id, "pass": int(a.pass_n), "prompt_hash": "p", "inputs_hash": h,
         "output": p["output"]}), encoding="utf-8")
else:
    text = " ".join(s["text"] for s in json.load(open(cache / f"{a.video_id}.json", encoding="utf-8"))["transcript"])
    bad = [it["quote"] for sec in json.load(sys.stdin)["by_section"].values()
           for k in ("concrete_claims", "vague_claims", "evidence_shown", "pitches")
           for it in sec.get(k, []) if it["quote"] not in text]
    for q in bad:
        sys.stderr.write(f"mismatch: {q}\n")
    sys.exit(1 if bad else 0)
'''

PASS1 = {"video_id": A, "sections": [{"id": "S1", "type": "content", "start": "0:00", "end": "10:00", "summary": ""}]}


def _pass2(quote):
    return {"video_id": A, "by_section": {"S1": {
        "concrete_claims": [{"timestamp": "0:30", "quote": quote, "paraphrase": "p"}],
        "vague_claims": [], "evidence_shown": [], "pitches": [{"timestamp": "9:00", "quote": "hello", "target": "x"}]}}}


@pytest.fixture
def claims(tmp_path):
    root = tmp_path / "youtube-claims"
    (root / "scripts").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "scripts" / "cache.py").write_text(STUB_CACHE_PY, encoding="utf-8")
    for n in ("extract_structure.md", "inventory_claims.md"):
        (root / "prompts" / n).write_text("prompt", encoding="utf-8")
    return root


def _wp(world, claims, n, out, *extra):  # noqa: F811
    p = world.cache / f"out{n}.json"
    p.write_text(json.dumps(out), encoding="utf-8")
    return run_tool("write-pass", n, A, "--output", p, "--claims-dir", claims, "--cache-dir", world.cache, *extra)


# -------------------------------------------------------------------- write-pass
def test_write_pass_builds_the_canonical_inputs_from_the_cache(world, claims):  # noqa: F811
    import hashlib
    (world.cache / f"{A}-pass2.json").unlink()
    r = _wp(world, claims, 1, PASS1)
    assert r.returncode == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["ok"] and out["sections"] == 1
    wrapper = json.loads((world.cache / f"{A}-pass1.json").read_text(encoding="utf-8"))
    fetch = json.loads((world.cache / f"{A}.json").read_text(encoding="utf-8"))
    want = hashlib.sha256(json.dumps({"transcript": fetch}, sort_keys=True).encode()).hexdigest()
    assert wrapper["inputs_hash"] == want and wrapper["output"] == PASS1


def test_write_pass_2_needs_pass_1_and_verified_quotes(world, claims, tmp_path):  # noqa: F811
    (world.cache / f"{A}-pass2.json").unlink()
    r = _wp(world, claims, 2, _pass2("hello there"))
    assert r.returncode == 2 and "write-pass 1 first" in r.stdout
    assert _wp(world, claims, 1, PASS1).returncode == 0
    bad = _wp(world, claims, 2, _pass2("not in the transcript"))
    assert bad.returncode == 2
    out = json.loads(bad.stdout)
    assert "verify-quotes failed" in out["error"] and any("not in the transcript" in ln for ln in out["verify_quotes"])
    assert not (world.cache / f"{A}-pass2.json").exists()
    tel = tmp_path / "tel"
    good = _wp(world, claims, 2, _pass2("hello there"), "--run-id", "r", "--model", "haiku",
               "--quote-fixes", 1, "--telemetry-dir", tel)
    assert good.returncode == 0, good.stdout
    out = json.loads(good.stdout)
    assert out["verified"] and out["items"] == 2 and out["telemetry"] == "logged"
    (ev,) = [json.loads(ln) for ln in (tel / "events.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert ev["event"] == "inventory" and ev["pass2_items"] == 2 and ev["verify_quotes_fail"] == 1
    assert ev["pass1_sections"] == 1 and ev["video"] == A


def test_write_pass_names_a_missing_claims_install(world, tmp_path):  # noqa: F811
    r = _wp(world, tmp_path / "nowhere", 1, PASS1)
    assert r.returncode == 2 and "youtube-claims not found" in r.stdout


# ------------------------------------------------------------------ draft-report
def _fill(text):
    return re.sub(r"<!--\s*WRITE\b.*?-->", "filled", text, flags=re.S)


def _check(world, text, *extra):  # noqa: F811
    p = world.analysis / "report.md"
    p.write_text(text, encoding="utf-8")
    return run_tool("check-report", "--analysis", world.analysis, "--report", p, "--cache-dir", world.cache, *extra)


def test_draft_report_passes_check_report_once_the_placeholders_are_filled(world):  # noqa: F811
    world.build("--score")
    r = run_tool("draft-report", "--analysis", world.analysis, "--cache-dir", world.cache)
    assert r.returncode == 0, r.stdout
    draft = r.stdout
    ro = json.loads((world.analysis / "rank_output.json").read_text(encoding="utf-8"))
    version = (SCRIPTS / "VERSION").read_text().strip()
    assert version in draft and f"| {ro['ranked'][0]['score']:.1f} |" in draft
    assert "2 of 3 key points were fact-checked" in draft
    assert "https://example.com" in draft and "SUPPORTED" in draft and "PARTLY" in draft
    assert "| 10:00 |" in draft and "| 20:00 |" in draft           # lengths from the caches
    unfilled = _check(world, draft)
    assert unfilled.returncode == 2 and "placeholder" in unfilled.stdout
    filled = _check(world, _fill(draft))
    assert filled.returncode == 0, filled.stdout


def test_draft_report_writes_to_a_file_and_refuses_to_overwrite(world):  # noqa: F811
    world.build("--score")
    out = world.analysis / "report.md"
    r = run_tool("draft-report", "--analysis", world.analysis, "--cache-dir", world.cache, "--output", out)
    assert r.returncode == 0 and json.loads(r.stdout)["placeholders"] > 5 and out.exists()
    again = run_tool("draft-report", "--analysis", world.analysis, "--cache-dir", world.cache, "--output", out)
    assert again.returncode == 2 and "--force" in again.stdout
    assert run_tool("draft-report", "--analysis", world.analysis, "--cache-dir", world.cache,
                    "--output", out, "--force").returncode == 0


# ----------------------------------------------------------------- single video
@pytest.fixture
def solo(world):  # noqa: F811
    kp = {"goal": "learn the thing", "videos": {A: {"relevance": 1.0, "reason": ""}},
          "points": [{"id": "K1", "text": "claim one", "core": True, "refs": [{"video": A, "ts": "0:30"}]},
                     {"id": "K2", "text": "claim two", "core": False, "refs": [{"video": A, "ts": "2:00"}]}]}
    vd = [{"id": "K1", "verdict": "PARTLY", "source_url": "https://example.com", "checked_at": "2026-11-01",
           "searches": 1}]
    world.write(kp, vd)
    assert world.build("--score").returncode == 0
    return world


def test_single_mode_drafts_a_read_without_a_score_and_checks_it(solo):
    r = run_tool("draft-report", "--analysis", solo.analysis, "--cache-dir", solo.cache, "--single")
    assert r.returncode == 0, r.stdout
    draft = r.stdout
    assert draft.startswith("# Standalone read:") and "| Rank |" not in draft and "Score" not in draft
    assert "1 of 2 key points were fact-checked" in draft
    assert _check(solo, _fill(draft), "--single").returncode == 0
    # the ranking checker refuses it (no Rank/Score table), so the modes do not blur
    assert _check(solo, _fill(draft)).returncode == 2
    # and the single checker still reads the counts
    wrong = _fill(draft).replace("| 10:00 |", "| 12:00 |")
    assert "table length" in _check(solo, wrong, "--single").stdout


def test_single_mode_refuses_a_batch(world):  # noqa: F811
    world.build("--score")
    r = run_tool("draft-report", "--analysis", world.analysis, "--cache-dir", world.cache, "--single")
    assert r.returncode == 2 and "exactly one ranked video" in r.stdout


# ------------------------------------------------------------- fact-check lookups
def test_build_input_warns_about_a_checked_verdict_with_no_lookup(world):  # noqa: F811
    vd = [{"id": "K1", "verdict": "SUPPORTED", "source_url": "https://example.com", "checked_at": "2026-11-01"},
          {"id": "K3", "verdict": "PARTLY", "source_url": "https://example.com", "checked_at": "2026-11-01",
           "searches": 0, "fetched": 1}]
    world.write(vd_=vd)
    warns = json.loads(world.build().stdout)["warnings"]
    assert any("K1" in w and "no search or fetch" in w for w in warns)
    assert not any("K3" in w and "no search or fetch" in w for w in warns)
