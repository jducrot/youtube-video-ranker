# youtube-video-ranker

A Claude skill that ranks several YouTube videos on one topic, so you can decide which are worth your time. It scores key points per minute, fact-checks the claims that matter against outside sources, weighs each video by how relevant it is to your goal, and returns a watch order with timestamped links.

This repo is the orchestration layer around an existing skill. It adds throttled fetching, cheap-model fan-out, relevance weighting, input validation, persisted analysis, and run telemetry, and it documents what each change did to cost and quality, including the changes that did not help.

## What it does

1. **Check the install** and the youtube-claims dependency, then start a run.
2. **Fetch** every transcript into a cache with bounded concurrency, trying the subtitle API first and `fetch.py` as backup (`prefetch.py`).
3. **Scan** the transcripts for instruction-like text aimed at an AI (`rank_tools.py scan-injection`).
4. **Inventory** each video's claims on a cheap model (Haiku), in parallel, from the cache only. Every quote is checked as a verbatim substring of the transcript.
5. **Merge** the claims into one shared list of key points, saved as `keypoints.json`.
6. **Fact-check** the core, checkable points with search subagents, preferring primary sources. Verdicts are saved with a source and a date.
7. **Score** with a deterministic script that validates its input first (`rank_tools.py build-input --score`, then `rank_videos.py`).
8. **Report** a recommendation, ranking table, watch plan, conflicts, and limits.
9. **Measure** the run with an append-only telemetry log (`telemetry.py`).

## Credits and dependencies

The per-video transcript, structure and claim-inventory work is done by the `youtube-claims` skill from [nishilbhave/youtube-inspector](https://github.com/nishilbhave/youtube-inspector) (its README states the MIT license). This repo does **not** copy that code. Install it separately:

```bash
npx skills add nishilbhave/youtube-inspector
```

## Install

Install it as a **plugin**, not by pasting `SKILL.md` into a skill editor. The skill depends on its `scripts/` folder, and a single-file upload arrives without it (that is exactly how an earlier version lost its scripts to a sync; Step 0 of the skill now detects it and stops with a clear message).

```bash
claude plugin marketplace add <path-or-github-user>/youtube-video-ranker
claude plugin install youtube-video-ranker@youtube-video-ranker
```

`claude plugin validate` passes on this folder. I have not installed it into a live setup yet. If you also have a skill of the same name in your account, disable one of them so the two don't compete.

You also need Python 3.11+ and `pip install yt-dlp youtube-transcript-api`. Tested on Windows with Git Bash only.

## Run the tests

```bash
python -m pip install pytest
python -m pytest
```

97 tests. They mock the network, so they need neither `yt-dlp` nor an internet connection. They cover URL parsing, caption de-overlap and its cache versioning, the fetch-order policy, the scorer and its validation, the input builder, the injection scan, telemetry, and a drift guard that fails if `SKILL.md` names a script or command that doesn't exist.

## What I measured

All numbers are from small runs by one user on one machine. See [docs/case-study.md](docs/case-study.md) for the full table, including what is still only an estimate.

- Inventory on Haiku produced **1 quote needing a fix in 558 items** (0.2%). Every cache passed `verify-quotes`.
- About **70% of inventory subagent tokens were a fixed per-agent cost**, not work that scales with the video (fit: ~87K tokens fixed + ~780 per item, R-squared 0.85).
- The default fetch path hit **20 rate-limit errors** in a 12-video batch. The fetch order now tries the subtitle API first; that was measured on one video (4.9 s, no errors) and has not been re-run on a full batch.
- Fact-checking 21 points found **3 contradicted, 7 partly right, 10 supported, 1 unverified**.

## What did not help

- **Caption de-overlap** made no net token difference in an A/B on two videos.
- **Moving the inventory brief into a file** did not reduce subagent tokens (97,374 vs 94,321 on the same video). It only shortens the orchestrator's launch prompts.

## Failures found in review (and fixed in 4.0.0)

A review of the run telemetry and the live files found these. Each was reproduced before it was fixed, and each now has a test.

- **The live skill had lost its scripts.** A sync replaced the local skill with a single-file copy. Now it ships as a plugin, and Step 0 refuses to run without its scripts.
- **The scorer dropped mistyped point IDs silently** (exit 0, no message, a plausible score). Now it is an error.
- **Scoring input was typed by hand and never saved.** Now key points and verdicts are saved, checked against the inventories, and turned into scoring input by a script.
- **Stage timings could lie.** Marks logged in a burst made one stage claim 99% of a run. Marks now carry a status, and the report warns about bursts.
- **Subagent briefs had no guard against instructions hidden in a transcript or web page.** Both briefs now have one, and Phase A scans for the phrases.
- **A cache fix could not be applied.** The cleaned transcript replaced the raw one. Now the raw cues are kept and the cleaner is versioned.

## Known limits

- The score counts checkable claims, and relevance is a rating the orchestrator assigns and the report shows, so it is a judgment call you can override.
- Only a sample of claims is fact-checked (about 20 per run). Creators' own results cannot be verified.
- The model/effort choices (Haiku for inventory, Sonnet for the orchestrator) rest on one A/B with one run per side.
- The injection scan matches a short list of phrases. It will miss a reworded attack; it is a prompt to look, not a defence on its own.
- There is no cross-run verdict cache yet. Reused verdicts are carried by hand with their original check date.
- Transcripts miss screen demos, and auto-captions can garble numbers.

## Repository layout

```
.claude-plugin/        plugin and marketplace manifests
skills/youtube-video-ranker/
  SKILL.md                    the instructions
  scripts/prefetch.py         throttled fetch, caption de-overlap with versioned cache
  scripts/rank_videos.py      the scorer, with input validation
  scripts/rank_tools.py       install check, injection scan, scoring-input builder
  scripts/telemetry.py        event log, agent events, stage marks, report
  scripts/inventory_brief.md  brief each inventory subagent follows
  scripts/factcheck_brief.md  brief each fact-check subagent follows
  scripts/VERSION             pipeline version
tests/                 pytest suite
docs/case-study.md     measured results, estimates, and the bugs found
examples/              a sample report and the telemetry log it came from
```

## License

MIT. See [LICENSE](LICENSE).
