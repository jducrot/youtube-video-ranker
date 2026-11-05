# Changelog

## 4.1.0

Driven by the telemetry and the report from a 6-video run (rifle zeroing), where the plan told the user to watch 38 minutes of which 13 were off-goal, the most actionable advice was never fact-checked, and two numbers in the report were wrong.

- **Segment-aware watch plan.** `build-input` turns each video's Pass 1 sections that hold key points into segments (sections under 90 s are joined to a neighbour). The scorer plans by segment and prints `watch_plan` (one step per segment) and `watch_ranges` (one entry per video: "watch 0:00 to 22:11"). It stops when nothing left adds 4% of the value (10% for a video with no sections) or adds value at under 20% of the best pick's per-minute rate. Candidates are filtered before the best is chosen, so a tiny fast segment cannot end the plan. Ranking scores are unchanged (checked on the real run). The 20% cutoff was set from one run.
- **Off-goal ranges.** `keypoints.json` may mark a stretch of a video with its own relevance (`ranges`), such as a pistol section in a rifle video. Those segments are weighted down in the plan. It is the orchestrator's judgment and is shown in the report.
- **Report checker.** `rank_tools.py check-report` compares a written report with `rank_output.json`, the verdicts and the caches: table order, score, counts, length and checked tally per video, watch-plan links, every outside URL, verdict tallies, "N of M key points" statements, the pipeline version and the required sections. `build-input` now prints per-video `facts` (concrete, evidence, vague and pitch counts and shares) so prose numbers are copied, not tallied. The checker reads tables, links and URLs, not sentences.
- **Inventory batching.** `rank_tools.py plan-inventory` groups videos of 10 minutes or less (at most 3 per agent, 24 minutes and 6,000 words in total) so they share one agent's fixed cost of about 70K tokens. The inventory brief takes `VIDEO_IDS` and replies with an array. `telemetry.py agent --videos` records a batch, and the report compares tokens per item and quote fixes for solo and batched agents. **Not yet tested on live agents**: the unit tests cover the planner, the brief wording and the report, not the quality of a batched inventory. The skill says to use it only where it saves two or more agents until a run shows batched quote fixes no worse than solo.
- **Fact-check scope.** Advice a viewer would act on ("two rounds is enough data") is now a candidate even when phrased as opinion, and the fact-check brief tells the checker to look for the authority (manual, standard, regulator, guideline). Unchecked actionable advice is named in the report's limits.
- **No-transcript scan.** `scan-injection` reports `"scanned": false` for a video with no transcript instead of "0 hits", and the telemetry event records `unscanned`.
- 129 tests.

## 4.0.0

Reliability release, driven by a review of the run telemetry and the live files.

- **Packaging.** Restructured as a Claude plugin (`.claude-plugin/`, `skills/youtube-video-ranker/`) so the scripts ship with the skill. Step 0 runs `rank_tools.py check-install` and stops with a clear message if the scripts are missing. `claude plugin validate` passes.
- **Scorer.** Moved out of `SKILL.md` into `scripts/rank_videos.py`. Unknown point ids, duplicate ids, bad verdicts and out-of-range relevance are now errors (exit 2) instead of silent drops. Warnings (a video with no points, orphan points) are returned in the output. Scores for valid input are unchanged; checked against four earlier real inputs.
- **Persisted analysis.** Key points and verdicts are saved under `~/youtube-reports/.analysis/<run>/`. `rank_tools.py build-input` checks every link to the inventories, takes durations and vague and pitch counts from the caches instead of hand tallies, and reports how much of each inventory the key points cover.
- **Telemetry.** Marks carry a status (`ok`, `skipped`, `cached`). The report warns when marks were logged in a burst.
- **Prompt injection.** Both subagent briefs carry an untrusted-input guard. New `factcheck_brief.md` replaces ad hoc fact-check prompts. `rank_tools.py scan-injection` counts instruction-like phrases in cached transcripts.
- **Cache versioning.** The caption cleaner is versioned (`_deoverlap_version`) and the raw cues are kept in `<id>.raw.json`, so a fix can be re-applied without re-fetching. Caches written by older code are left alone.
- **Instructions.** Merged the browser-tab rules added to the account-side copy of the skill. Added verdict age and expiry guidance.
- 97 tests (was 47), including a guard that fails if `SKILL.md` references a missing script or command.

## 3.x

Throttled two-phase pipeline, relevance weighting, telemetry, brief-by-file, caption cleaner with word-boundary fix.
