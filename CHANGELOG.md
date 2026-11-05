# Changelog

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
