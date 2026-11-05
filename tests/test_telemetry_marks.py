"""Stage marks: a status per mark, and a warning when marks were logged in a burst
(which made one real timeline claim 99% of a run happened in a single stage)."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest


@pytest.fixture
def tel(telemetry_path, tmp_path):
    d = tmp_path / "tel"

    def run(*args):
        p = subprocess.run([sys.executable, "-I", str(telemetry_path), "--telemetry-dir", str(d),
                            *map(str, args)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        return p

    run.dir = d
    run.events = lambda: [json.loads(x) for x in
                          (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if x]
    return run


def _write_events(tel, events):
    tel.dir.mkdir(parents=True, exist_ok=True)
    (tel.dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _marks(run, t0, names_and_offsets, status="ok"):
    ev = [{"ts": t0, "run_id": run, "event": "run_start", "n_videos": 2}]
    for name, off in names_and_offsets:
        ev.append({"ts": t0 + off, "run_id": run, "event": "mark", "name": name, "status": status})
    ev.append({"ts": t0 + names_and_offsets[-1][1] + 1, "run_id": run, "event": "run_end"})
    return ev


def test_mark_records_a_status(tel):
    tel("mark", "--run-id", "r", "--name", "fetch_done")
    tel("mark", "--run-id", "r", "--name", "factcheck_done", "--status", "skipped")
    a, b = tel.events()
    assert a["status"] == "ok" and b["status"] == "skipped"


def test_report_with_realistic_spacing_has_no_warning(tel):
    _write_events(tel, _marks("r", 1000.0, [("fetch_done", 5), ("inventory_done", 60), ("factcheck_done", 60.1)]))
    out = tel("report", "--run-id", "r").stdout
    assert "inventory_done" in out and "WARNING" not in out


def test_report_warns_when_marks_were_logged_in_a_burst(tel):
    burst = [("fetch_done", 0.4), ("goal_settled", 40.1), ("inventory_done", 40.2),
             ("keypoints_done", 40.4), ("factcheck_done", 40.5), ("scored", 40.7)]
    _write_events(tel, _marks("r", 1000.0, burst))
    out = tel("report", "--run-id", "r").stdout
    assert "WARNING" in out and "logged in a batch" in out


def test_a_burst_of_cached_marks_is_honest_not_a_warning(tel):
    burst = [("fetch_done", 0.4), ("inventory_done", 0.5), ("keypoints_done", 0.6), ("factcheck_done", 0.7)]
    _write_events(tel, _marks("r", 1000.0, burst, status="cached"))
    out = tel("report", "--run-id", "r").stdout
    assert "WARNING" not in out and "[cached]" in out


def test_two_close_marks_alone_are_not_enough_to_warn(tel):
    _write_events(tel, _marks("r", 1000.0, [("fetch_done", 10), ("goal_settled", 10.5), ("scored", 40)]))
    assert "WARNING" not in tel("report", "--run-id", "r").stdout
