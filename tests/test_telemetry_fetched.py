"""telemetry.py: fact-check verdicts record how many pages were fetched, and the report
flags a verdict that recorded no lookup at all."""
from __future__ import annotations

import json

from test_telemetry import tel  # noqa: F401  (fixture)


def test_ingest_records_fetches_and_the_report_flags_verdicts_with_no_lookup(tel, tmp_path):  # noqa: F811
    f = tmp_path / "verdicts.json"
    f.write_text(json.dumps([
        {"id": "K1", "verdict": "SUPPORTED", "searches": 0, "fetched": 1},
        {"id": "K2", "verdict": "PARTLY", "searches": 0},
        {"id": "K3", "verdict": "UNVERIFIED", "searches": 2}]), encoding="utf-8")
    tel("log", "--run-id", "r", "--stage", "run", "--event", "run_start", "--kv", "n_videos=1")
    tel("ingest-factcheck", "--run-id", "r", "--file", f)
    evs = tel.events()
    assert next(e for e in evs if e.get("id") == "K1")["fetched"] == 1
    out = tel("report", "--run-id", "r").stdout
    assert "fetches=1" in out and "1 verdict(s) with no search or fetch" in out
