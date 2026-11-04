# youtube-video-ranker

A Claude skill that ranks several YouTube videos on one topic, so you can decide which are worth your time. It scores key points per minute, fact-checks the claims that matter against outside sources, and returns a watch order with timestamped links.

This repo is the orchestration layer around two existing pieces. It adds throttled fetching, cheap-model fan-out, relevance weighting, and run telemetry, and it documents what each change did to cost and quality, including the changes that did not help.

## What it does

1. **Fetch** every transcript into a cache, with bounded concurrency and a rate-limit-aware fallback (`skill/scripts/prefetch.py`).
2. **Inventory** each video's claims on a cheap model (Haiku), in parallel, from the cache only. Every quote is checked as a verbatim substring of the transcript.
3. **Merge** the claims into one shared list of key points across videos.
4. **Fact-check** the core, checkable points with search subagents, preferring primary sources.
5. **Score** each video with a deterministic script: validity, coverage, density, signal, then a relevance factor and a penalty for contradicted core points.
6. **Report** a recommendation, ranking table, watch plan, conflicts, and limits.
7. **Measure** the run with an append-only telemetry log (`skill/scripts/telemetry.py`).

## Credits and dependencies

The per-video transcript, structure and claim-inventory work is done by the `youtube-claims` skill from [nishilbhave/youtube-inspector](https://github.com/nishilbhave/youtube-inspector) (its README states the MIT license). This repo does **not** copy that code. Install it separately:

```bash
npx skills add nishilbhave/youtube-inspector
```

## Install

1. Install `youtube-inspector` as above.
2. Python 3.11 or newer, plus `pip install yt-dlp youtube-transcript-api`.
3. Copy the `skill/` folder into your skills directory as `youtube-video-ranker/`.
4. Ask Claude to rank some video URLs, for example "rank these videos for learning X".

Tested on Windows with Git Bash only. The scripts avoid platform-specific paths, but I have not run the full pipeline on macOS or Linux.

## Run the tests

```bash
python -m pip install pytest
python -m pytest
```

The tests mock the network, so they need neither `yt-dlp` nor an internet connection. They cover URL parsing, caption de-overlap, the fetch-order policy, the scorer, and telemetry.

## What I measured

All numbers are from small runs by one user on one machine (a 12-video batch plus a few 1 to 2 video experiments). See [docs/case-study.md](docs/case-study.md) for the full table, including what is still only an estimate.

- Inventory on Haiku produced **1 quote needing a fix in 558 items** (0.2%). Every cache passed `verify-quotes`.
- About **70% of inventory subagent tokens were a fixed per-agent cost**, not work that scales with the video (fit: ~87K tokens fixed + ~780 per item, R-squared 0.85).
- The default fetch path hit **20 rate-limit errors** in the 12-video batch. The fetch order now tries the subtitle API first; that was measured on one video (4.9 s, no errors) and has not been re-run on a full batch.
- Fact-checking 21 points found **3 contradicted, 7 partly right, 10 supported, 1 unverified**.

## What did not help

- **Caption de-overlap** made no net token difference in an A/B on two videos.
- **Moving the inventory brief into a file** did not reduce subagent tokens (97,374 vs 94,321 on the same video). It only shortens the orchestrator's launch prompts.

## Known limits

- The score counts checkable claims, not relevance, so a general roundup can outrank a focused video. The relevance factor corrects this, but the relevance ratings are a judgment call that the skill asks you to review.
- Only a sample of claims is fact-checked (about 20 per run). Creators' own results cannot be verified.
- The model/effort choices (Haiku for inventory, Sonnet for the orchestrator) rest on one A/B with one run per side.
- Transcripts miss screen demos, and auto-captions can garble numbers.

## Repository layout

```
skill/                 the skill: SKILL.md plus scripts/
  scripts/prefetch.py        throttled fetch, caption de-overlap
  scripts/telemetry.py       event log, harness-number agent events, report
  scripts/inventory_brief.md the brief each inventory subagent follows
tests/                 pytest suite
docs/case-study.md     measured results, estimates, and the bug the tests found
examples/              a sample report and the telemetry log it came from
```

## License

MIT. See [LICENSE](LICENSE).
