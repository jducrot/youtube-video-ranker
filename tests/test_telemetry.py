"""telemetry.py: append-only log, harness-number agent events, ingest, report."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest


@pytest.fixture
def tel(telemetry_path, tmp_path):
    d = tmp_path / "tel"

    def run(*args, check=True):
        p = subprocess.run([sys.executable, "-I", str(telemetry_path),
                            "--telemetry-dir", str(d), *map(str, args)],
                           capture_output=True, text=True)
        if check:
            assert p.returncode == 0, p.stderr
        return p

    run.dir = d
    run.events = lambda: [json.loads(x) for x in
                          (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if x]
    return run


def test_log_appends_one_typed_json_line(tel):
    tel("log", "--run-id", "r", "--stage", "run", "--event", "run_start",
        "--kv", "n_videos=12", "model=sonnet", "ratio=0.5", "ok=true")
    (e,) = tel.events()
    assert e["n_videos"] == 12 and e["ratio"] == 0.5 and e["ok"] is True
    assert e["model"] == "sonnet" and e["event"] == "run_start"


def test_mark_and_agent_events(tel):
    tel("mark", "--run-id", "r", "--name", "fetch_done")
    tel("agent", "--run-id", "r", "--kind", "inventory", "--video", "v1",
        "--model", "haiku", "--tokens", "97374", "--tool-uses", "11", "--duration-s", "68.44")
    mark, agent = tel.events()
    assert mark["event"] == "mark" and mark["name"] == "fetch_done"
    assert agent["tokens"] == 97374 and agent["duration_s"] == 68.4 and agent["tool_uses"] == 11


def test_ingest_factcheck_counts_and_skips_junk(tel, tmp_path):
    f = tmp_path / "fc.json"
    f.write_text(json.dumps([
        {"id": "K1", "verdict": "PARTLY", "searches": 1},
        {"id": "K2", "verdict": "SUPPORTED", "searches": 2, "cache_hit": 1},
        {"id": "K3"},                      # no verdict: skipped
        "not a dict"]), encoding="utf-8")
    p = tel("ingest-factcheck", "--run-id", "r", "--file", f, "--model", "sonnet")
    assert "ingested 2" in p.stdout and "3 searches" in p.stdout
    ev = tel.events()
    assert [e["id"] for e in ev] == ["K1", "K2"]
    assert ev[1]["cache_hit"] == 1 and ev[0]["cache_hit"] == 0


def test_ingest_factcheck_fails_loudly_when_nothing_ingested(tel, tmp_path):
    f = tmp_path / "empty.json"
    f.write_text("[]", encoding="utf-8")
    assert tel("ingest-factcheck", "--run-id", "r", "--file", f, check=False).returncode == 1


def _seed_run(tel, fixed=80_000, per_item=700, n=6):
    tel("log", "--run-id", "r", "--stage", "run", "--event", "run_start",
        "--kv", f"n_videos={n}", "model=sonnet", "concurrency=3")
    for i in range(n):
        items = 10 + 15 * i
        tel("log", "--run-id", "r", "--stage", "inventory", "--event", "inventory",
            "--video", f"v{i}", "--model", "haiku",
            "--kv", "ok=1", f"pass2_items={items}", "verify_quotes_fail=0")
        tel("agent", "--run-id", "r", "--kind", "inventory", "--video", f"v{i}",
            "--model", "haiku", "--tokens", fixed + per_item * items, "--duration-s", 60)
    tel("mark", "--run-id", "r", "--name", "inventory_done")
    tel("log", "--run-id", "r", "--stage", "run", "--event", "run_end",
        "--kv", f"n_ranked={n}", "n_rejected=0")


def test_report_recovers_the_fixed_cost_of_an_agent(tel):
    _seed_run(tel, fixed=80_000, per_item=700)
    out = tel("report", "--run-id", "r").stdout
    assert "~80,000 tokens fixed per agent" in out
    assert "~700 per item" in out and "R2=1.00" in out
    assert "config  n_videos=6  model=sonnet  concurrency=3" in out


def test_report_shows_timeline_and_flags_missing_marks(tel):
    _seed_run(tel)
    assert "TIMELINE (seconds since run_start" in tel("report", "--run-id", "r").stdout
    tel("log", "--run-id", "bare", "--stage", "run", "--event", "run_start", "--kv", "n_videos=1")
    assert "no stage marks logged" in tel("report", "--run-id", "bare").stdout


def test_report_survives_a_torn_final_line(tel):
    _seed_run(tel)
    with (tel.dir / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "run_id": "r", "event": "fetc')   # half-written line
    assert "run r" in tel("report", "--run-id", "r").stdout


def test_report_with_no_data_is_a_message_not_a_crash(tel):
    assert "No telemetry" in tel("report").stdout
