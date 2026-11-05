"""telemetry.py: agents that inventoried a batch of videos, and the solo-vs-batched report."""
from __future__ import annotations

from test_telemetry import tel  # noqa: F401  (fixture)


def test_agent_records_a_batch_of_videos(tel):  # noqa: F811
    tel("agent", "--run-id", "r", "--kind", "inventory", "--videos", "v1,v2,v3",
        "--model", "haiku", "--tokens", "120000", "--duration-s", "90")
    (e,) = tel.events()
    assert e["videos"] == ["v1", "v2", "v3"] and e["batch_size"] == 3 and e["video"] == "v1"


def test_report_compares_solo_and_batched_agents(tel):  # noqa: F811
    tel("log", "--run-id", "r", "--stage", "run", "--event", "run_start", "--kv", "n_videos=5")
    for vid, items, vq in (("v0", 40, 0), ("v1", 60, 0), ("v2", 20, 0), ("v3", 10, 1), ("v4", 12, 0)):
        tel("log", "--run-id", "r", "--stage", "inventory", "--event", "inventory", "--video", vid,
            "--model", "haiku", "--kv", "ok=1", f"pass2_items={items}", f"verify_quotes_fail={vq}")
    for vid, tokens in (("v0", 110000), ("v1", 130000), ("v2", 95000)):
        tel("agent", "--run-id", "r", "--kind", "inventory", "--video", vid, "--model", "haiku",
            "--tokens", tokens, "--duration-s", 60)
    tel("agent", "--run-id", "r", "--kind", "inventory", "--videos", "v3,v4", "--model", "haiku",
        "--tokens", 100000, "--duration-s", 80)
    out = tel("report", "--run-id", "r").stdout
    assert "solo" in out and "batched" in out
    assert "tokens/item=" in out and "quote fixes=1" in out
