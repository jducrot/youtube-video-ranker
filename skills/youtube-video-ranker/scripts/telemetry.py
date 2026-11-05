#!/usr/bin/env python3
"""Append-only telemetry for youtube-video-ranker runs, plus an aggregator.

Design goals:
- Deterministic and local. No model calls, no network. Safe to run with ``-I``.
- Append-only JSONL so concurrent writers (per-video subagents) never clobber
  each other. Each write appends exactly one line.
- Only log numbers that something actually MEASURED. A model cannot time or
  count its own tokens, so subagents must not report ``seconds`` or tokens.
  The orchestrator logs those from the harness's completion notice with the
  ``agent`` command, and marks stage boundaries with ``mark`` so wall time is
  attributable.

Commands (all accept --telemetry-dir, default ~/youtube-reports/.telemetry):

  log      generic event: --run-id R --stage S [--event E --video V --model M --kv k=v ...]
  mark     stage boundary: --run-id R --name fetch_done [--status ok|skipped|cached] [--kv ...]
           Mark a stage when it ENDS. Use --status skipped/cached for a stage that did
           no work (a cache-only re-run), so the timeline does not invent durations.
  agent    one finished subagent, numbers copied from the harness completion notice:
             --run-id R --kind inventory|factcheck [--video V] --model M
             --tokens N --duration-s S [--tool-uses T] [--points P]
  ingest-factcheck
           read fact-check results (a JSON array of {id, verdict, searches, ...},
           or several arrays concatenated into one list) and write one
           ``factcheck`` event per point, so nobody retypes verdicts:
             --run-id R --file results.json [--model M]
  report   [--run-id R] [--last N]   aggregate into a readable summary
  path     print the events.jsonl path

Recommended marks, in order: fetch_done, goal_settled, inventory_done,
keypoints_done, factcheck_done, scored, report_done. Put run configuration on
``run_start`` (--kv n_videos=12 model=sonnet effort=medium concurrency=3) so
runs are comparable.

Event vocabulary the report understands:
  run_start/run_end, mark(name), fetch(video, method, ok, http_429, retries,
  transcript_bytes, seconds, status), inventory(video, model, ok, pass1_sections,
  pass2_items, verify_quotes_fail), agent(kind, video, model, tokens, duration_s,
  tool_uses, points), factcheck(id, verdict, searches, cache_hit).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DIR = Path.home() / "youtube-reports" / ".telemetry"
EVENTS_FILE = "events.jsonl"
BATCH_GAP_SECONDS = 2.0   # marks closer together than this look batch-logged
BATCH_RUN_LENGTH = 3      # ...if at least this many in a row


def _events_path(d: Path) -> Path:
    return d / EVENTS_FILE


def _coerce(v: str):
    """Turn a k=v string value into int/float/bool where it clearly is one."""
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _append(d: Path, rec: dict) -> str:
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False)
    with _events_path(d).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def _kv(pairs) -> dict:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            sys.stderr.write(f"telemetry: ignoring malformed kv {pair!r}\n")
            continue
        k, v = pair.split("=", 1)
        out[k] = _coerce(v)
    return out


def _base(args, stage, event) -> dict:
    return {"ts": round(time.time(), 3), "run_id": args.run_id,
            "stage": stage, "event": event}


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def cmd_log(args: argparse.Namespace) -> int:
    rec = _base(args, args.stage, args.event or args.stage)
    if args.video:
        rec["video"] = args.video
    if args.model:
        rec["model"] = args.model
    rec.update(_kv(args.kv))
    line = _append(Path(args.telemetry_dir), rec)
    if args.echo:
        sys.stdout.write(line + "\n")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    rec = _base(args, "run", "mark")
    rec["name"] = args.name
    rec["status"] = args.status
    rec.update(_kv(args.kv))
    line = _append(Path(args.telemetry_dir), rec)
    if args.echo:
        sys.stdout.write(line + "\n")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    rec = _base(args, args.kind, "agent")
    rec["kind"] = args.kind
    if args.video:
        rec["video"] = args.video
    if args.model:
        rec["model"] = args.model
    rec["tokens"] = args.tokens
    rec["duration_s"] = round(args.duration_s, 1)
    if args.tool_uses is not None:
        rec["tool_uses"] = args.tool_uses
    if args.points is not None:
        rec["points"] = args.points
    rec.update(_kv(args.kv))
    line = _append(Path(args.telemetry_dir), rec)
    if args.echo:
        sys.stdout.write(line + "\n")
    return 0


def cmd_ingest_factcheck(args: argparse.Namespace) -> int:
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"telemetry: cannot read {args.file}: {e}\n")
        return 1
    if isinstance(data, dict):
        data = [data]
    d = Path(args.telemetry_dir)
    n = searches = 0
    for item in data:
        if not isinstance(item, dict) or "verdict" not in item:
            continue
        rec = _base(args, "factcheck", "factcheck")
        rec["id"] = item.get("id")
        rec["verdict"] = item.get("verdict")
        rec["searches"] = int(item.get("searches", 0) or 0)
        rec["cache_hit"] = 1 if item.get("cache_hit") in (1, True) else 0
        if args.model:
            rec["model"] = args.model
        _append(d, rec)
        n += 1
        searches += rec["searches"]
    print(f"ingested {n} fact-check results, {searches} searches")
    return 0 if n else 1


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _load(d: Path):
    p = _events_path(d)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # tolerate a torn final line
    return out


def _fmt_secs(s):
    if s is None:
        return "-"
    return f"{s:.0f}s" if s < 100 else f"{s/60:.1f}m"


def _ols(xs, ys):
    """Least-squares y = a + b*x. Returns (a, b, r2) or None."""
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return a, b, r2


def _last_per_video(evs):
    d = {}
    for e in sorted(evs, key=lambda e: e.get("ts", 0)):
        d[e.get("video")] = e
    return list(d.values())


def _report_run(rid, evs):
    evs = sorted(evs, key=lambda e: e.get("ts", 0))
    rs = next((e for e in evs if e.get("event") == "run_start"), None)
    re_ = next((e for e in reversed(evs) if e.get("event") == "run_end"), None)
    t0 = rs["ts"] if rs else evs[0]["ts"]
    t1 = re_["ts"] if re_ else evs[-1]["ts"]
    span = t1 - t0

    print("=" * 66)
    print(f"run {rid}   wall {_fmt_secs(span)}   events {len(evs)}")
    if rs:
        cfg = {k: v for k, v in rs.items()
               if k not in ("ts", "run_id", "stage", "event")}
        if cfg:
            print("config  " + "  ".join(f"{k}={v}" for k, v in cfg.items()))
    print("-" * 66)

    # TIMELINE: where did the wall-clock go?
    marks = [e for e in evs if e.get("event") == "mark"]
    if marks:
        print("TIMELINE (seconds since run_start; share of wall)")
        prev = t0
        run_fast, worst_run = 0, 0
        for m in marks:
            dt = m["ts"] - prev
            share = (dt / span * 100) if span > 0 else 0
            status = m.get("status", "ok")
            tag = "" if status == "ok" else f"  [{status}]"
            print(f"        {m.get('name','?'):<16} at {m['ts']-t0:6.1f}s   "
                  f"+{dt:5.1f}s  {share:3.0f}%{tag}")
            # Marks that claim real work but arrive under 2 s after the previous one
            # were almost certainly logged in a batch; their durations mean nothing.
            if status == "ok" and dt < BATCH_GAP_SECONDS:
                run_fast += 1
                worst_run = max(worst_run, run_fast)
            else:
                run_fast = 0
            prev = m["ts"]
        if re_ and t1 - prev > 1:
            print(f"        {'(after last mark)':<16} at {span:6.1f}s   "
                  f"+{t1-prev:5.1f}s  {(t1-prev)/span*100:3.0f}%")
        if worst_run >= BATCH_RUN_LENGTH:
            print(f"        WARNING: {worst_run} consecutive marks arrived within "
                  f"{BATCH_GAP_SECONDS:.0f}s of each other. They were probably logged in a "
                  "batch, so the durations above are not real. Mark each stage when it ends, "
                  "or use --status skipped for stages that did not run.")
    else:
        print("TIMELINE  no stage marks logged; wall time is not attributable")

    # FETCH
    fetches = [e for e in evs if e.get("event") == "fetch"]
    if fetches:
        methods = Counter(f.get("method", "?") for f in fetches)
        n429 = sum(int(f.get("http_429", 0)) for f in fetches)
        retries = sum(int(f.get("retries", 0)) for f in fetches)
        okc = sum(1 for f in fetches if f.get("ok") in (1, True))
        tb = sum(int(f.get("transcript_bytes", 0)) for f in fetches)
        secs = [float(f.get("seconds", 0) or 0) for f in fetches]
        print(f"FETCH   {len(fetches)} videos  ok={okc}  429s={n429}  retries={retries}  "
              f"transcript={tb/1024:.0f}KB  per-video max={max(secs):.0f}s")
        print("        methods: " + ", ".join(f"{k}={v}" for k, v in methods.items()))

    # INVENTORY quality (counts only; durations come from agent events)
    invs = _last_per_video([e for e in evs if e.get("event") == "inventory"])
    if invs:
        per_model = defaultdict(lambda: {"n": 0, "vq": 0, "items": 0, "ok": 0})
        for e in invs:
            m = per_model[e.get("model", "?")]
            m["n"] += 1
            m["vq"] += int(e.get("verify_quotes_fail", 0))
            m["items"] += int(e.get("pass2_items", 0))
            m["ok"] += 1 if e.get("ok") in (1, True) else 0
        print("INVENTORY (verify-quotes failures = cheap-model quality signal)")
        for model, m in per_model.items():
            rate = (m["vq"] / m["items"] * 100) if m["items"] else 0.0
            print(f"        model={model}  videos={m['n']}  ok={m['ok']}  "
                  f"items={m['items']}  vq_fail={m['vq']} ({rate:.1f}% of quotes)")

    # AGENTS: measured tokens/duration copied from the harness
    agents = [e for e in evs if e.get("event") == "agent"]
    if agents:
        print("AGENTS  (tokens and duration are harness numbers, not self-reported)")
        for kind in sorted({a.get("kind", "?") for a in agents}):
            ag = [a for a in agents if a.get("kind") == kind]
            toks = [int(a.get("tokens", 0)) for a in ag]
            durs = [float(a.get("duration_s", 0)) for a in ag]
            print(f"        {kind:<10} n={len(ag)}  tokens total={sum(toks):,} "
                  f"mean={sum(toks)//len(toks):,} max={max(toks):,}  "
                  f"agent-time sum={_fmt_secs(sum(durs))} max={_fmt_secs(max(durs))}")
            # fixed-cost floor: tokens ~ a + b*units
            if kind == "inventory":
                items = {e.get("video"): int(e.get("pass2_items", 0)) for e in invs}
                pairs = [(items[a["video"]], int(a["tokens"])) for a in ag
                         if a.get("video") in items]
                fit = _ols([p[0] for p in pairs], [p[1] for p in pairs])
                if fit:
                    a0, b, r2 = fit
                    fixed = a0 * len(pairs)
                    tot = sum(p[1] for p in pairs)
                    print(f"                   cost model: ~{a0:,.0f} tokens fixed per agent "
                          f"+ ~{b:,.0f} per item (R2={r2:.2f}); "
                          f"fixed share ~{fixed/tot*100:.0f}% of inventory tokens")
            else:
                pts = sum(int(a.get("points", 0) or 0) for a in ag)
                if pts:
                    print(f"                   {sum(toks)//pts:,} tokens per checked point")

    # FACTCHECK funnel
    facts = [e for e in evs if e.get("event") == "factcheck"]
    if facts:
        verdicts = Counter(f.get("verdict", "?") for f in facts)
        searches = sum(int(f.get("searches", 0)) for f in facts)
        chits = sum(1 for f in facts if f.get("cache_hit") in (1, True))
        print(f"FACTCHECK  checked={len(facts)}  searches={searches}  cache_hits={chits}")
        print("        verdicts: " + ", ".join(f"{k}={v}" for k, v in verdicts.items()))

    nv = rs.get("n_videos") if rs else len(fetches)
    items = sum(int(e.get("pass2_items", 0)) for e in invs)
    extra = (f"  ranked={re_.get('n_ranked')} rejected={re_.get('n_rejected')}" if re_ else "")
    print(f"PROXIES  videos={nv}  pass2_items={items}{extra}")


def cmd_report(args: argparse.Namespace) -> int:
    d = Path(args.telemetry_dir)
    events = _load(d)
    if not events:
        print(f"No telemetry at {_events_path(d)}")
        return 0
    by_run = defaultdict(list)
    for e in events:
        by_run[e.get("run_id", "?")].append(e)
    run_ids = list(by_run.keys())
    if args.run_id:
        run_ids = [r for r in run_ids if r == args.run_id]
    else:
        run_ids.sort(key=lambda r: min(e.get("ts", 0) for e in by_run[r]))
        if args.last:
            run_ids = run_ids[-args.last:]
    for rid in run_ids:
        _report_run(rid, by_run[rid])
    print("=" * 66)
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    print(_events_path(Path(args.telemetry_dir)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--telemetry-dir", default=str(DEFAULT_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("log", help="append one generic event")
    pl.add_argument("--run-id", required=True)
    pl.add_argument("--stage", required=True)
    pl.add_argument("--event", default=None)
    pl.add_argument("--video", default=None)
    pl.add_argument("--model", default=None)
    pl.add_argument("--kv", nargs="*", default=None)
    pl.add_argument("--echo", action="store_true")
    pl.set_defaults(func=cmd_log)

    pm = sub.add_parser("mark", help="record a stage boundary")
    pm.add_argument("--run-id", required=True)
    pm.add_argument("--name", required=True)
    pm.add_argument("--status", choices=("ok", "skipped", "cached"), default="ok",
                    help="'skipped' or 'cached' for a stage that did no real work this run")
    pm.add_argument("--kv", nargs="*", default=None)
    pm.add_argument("--echo", action="store_true")
    pm.set_defaults(func=cmd_mark)

    pa = sub.add_parser("agent", help="record a finished subagent (harness numbers)")
    pa.add_argument("--run-id", required=True)
    pa.add_argument("--kind", required=True)
    pa.add_argument("--video", default=None)
    pa.add_argument("--model", default=None)
    pa.add_argument("--tokens", type=int, required=True)
    pa.add_argument("--duration-s", type=float, required=True)
    pa.add_argument("--tool-uses", type=int, default=None)
    pa.add_argument("--points", type=int, default=None)
    pa.add_argument("--kv", nargs="*", default=None)
    pa.add_argument("--echo", action="store_true")
    pa.set_defaults(func=cmd_agent)

    pi = sub.add_parser("ingest-factcheck", help="write factcheck events from a results file")
    pi.add_argument("--run-id", required=True)
    pi.add_argument("--file", required=True)
    pi.add_argument("--model", default=None)
    pi.set_defaults(func=cmd_ingest_factcheck)

    pr = sub.add_parser("report", help="aggregate events into a readable summary")
    pr.add_argument("--run-id", default=None)
    pr.add_argument("--last", type=int, default=5)
    pr.set_defaults(func=cmd_report)

    pp = sub.add_parser("path", help="print the events.jsonl path")
    pp.set_defaults(func=cmd_path)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
