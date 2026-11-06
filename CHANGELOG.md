# Changelog

## 4.2.0

Driven by the telemetry of five runs since 4.0.0 and by two single-video runs that both mis-logged.

- **`write-pass`: the inventory agent no longer copies the transcript.** youtube-claims' `cache.py write` takes `{inputs, output}` and stores only a hash of the inputs, so each inventory agent was writing the whole transcript into a wrapper file twice per video just to be hashed. The telemetry cost model put the fixed cost at about 73K tokens per agent, 63% of inventory spend. `rank_tools.py write-pass {1|2} VIDEO_ID --output FILE --claims-dir DIR` builds the canonical inputs from the cache, runs verify-quotes before a Pass 2 write, writes the cache and (with `--run-id`) logs the inventory telemetry event. Checked against the real caches: the wrappers it writes carry the same `inputs_hash` and `prompt_hash` as the ones the agents produced, so the caches stay shared with youtube-claims. The inventory brief shrinks to two commands per video. The saving is a prediction until the next live run shows the fixed term fall.
- **`draft-report`: the report's numbers are written by code.** It fills the header, the ranking table, the watch plan and its links, the conflicts table with every checked verdict and source, the per-video inventory counts and the limits facts from `rank_output.json`, the verdicts and the caches, and leaves `<!-- WRITE: ... -->` placeholders where judgment goes. `check-report` refuses a report that still holds a placeholder. In earlier runs the report stage took 1.3 to 1.7 minutes and produced every error the checker exists to catch. The draft of the real 2-video run passes `check-report` as soon as its placeholders are filled.
- **Standalone read of one video.** `draft-report --single` and `check-report --single` (no rank, score or relevance; sections Verdict, The video, Watch plan, Claims checked, Notes, Limits), and a `Standalone read` section in `SKILL.md` that runs the same pipeline with the comparison parts switched off, so a single-video run leaves a reusable `verdicts.json`, a report and honest telemetry.
- **Fact-check agents are grouped by size, not by source kind.** Measured about 65K tokens fixed per agent plus about 11K per point, so the skill now asks for two or three agents of 7 to 10 points. The brief reports `fetched` beside `searches`, and a checked verdict with no search or fetch is flagged by `build-input`, by `telemetry.py report`, and by the skill (treat it as `UNVERIFIED`). A live verdict had come back with `searches: 0`.
- 139 tests.

## 4.1.0

Driven by the telemetry and the report from a 6-video run (rifle zeroing), where the plan told the user to watch 38 minutes of which 13 were off-goal, the most actionable advice was never fact-checked, and two numbers in the report were wrong.

- **Segment-aware watch plan.** `build-input` turns each video's Pass 1 sections that hold key points into segments (sections under 90 s are joined to a neighbour). The scorer plans by segment and prints `watch_plan` (one step per segment) and `watch_ranges` (one entry per video: "watch 0:00 to 22:11"). It stops when nothing left adds 4% of the value (10% for a video with no sections) or adds value at under 20% of the best pick's per-minute rate. Candidates are filtered before the best is chosen, so a tiny fast segment cannot end the plan. Ranking scores are unchanged (checked on the real run). The 20% cutoff was set from one run, and a second run showed a flaw: a 1.7-minute opening set such a high rate that the 7-minute core tutorial section fell under the cutoff. Segments shorter than 3 minutes are now compared as if they were 3 minutes long (`RATE_MIN_MINUTES`), with a regression test. Both real runs were re-scored: ranking scores are unchanged.
- **Off-goal ranges.** `keypoints.json` may mark a stretch of a video with its own relevance (`ranges`), such as a pistol section in a rifle video. Those segments are weighted down in the plan. It is the orchestrator's judgment and is shown in the report.
- **Report checker.** `rank_tools.py check-report` compares a written report with `rank_output.json`, the verdicts and the caches: table order, score, counts, length and checked tally per video, watch-plan links, every outside URL, verdict tallies, "N of M key points" statements, the pipeline version and the required sections. `build-input` now prints per-video `facts` (concrete, evidence, vague and pitch counts and shares) so prose numbers are copied, not tallied. The checker reads tables, links and URLs, not sentences.
- **Inventory batching.** `rank_tools.py plan-inventory` groups videos of 10 minutes or less (at most 3 per agent, 24 minutes and 6,000 words in total) so they share one agent's fixed cost of about 70K tokens. The inventory brief takes `VIDEO_IDS` and replies with an array. `telemetry.py agent --videos` records a batch, and the report compares tokens per item and quote fixes for solo and batched agents. **Not yet tested on live agents**: the unit tests cover the planner, the brief wording and the report, not the quality of a batched inventory. The skill says to use it only where it saves two or more agents until a run shows batched quote fixes no worse than solo.
- **Fact-check scope.** Advice a viewer would act on ("two rounds is enough data") is now a candidate even when phrased as opinion, and the fact-check brief tells the checker to look for the authority (manual, standard, regulator, guideline). Unchecked actionable advice is named in the report's limits.
- **No-transcript scan.** `scan-injection` reports `"scanned": false` for a video with no transcript instead of "0 hits", and the telemetry event records `unscanned`.
- 130 tests.

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
