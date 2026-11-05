# Case study: making a multi-agent skill cheaper and measurable

The starting point was a skill that worked but was slow and expensive on a 2-video run. The goal was to run 10 to 15 videos without rate-limit trouble, to know where the cost went, and to find out which model and effort level were good enough.

Everything here comes from small runs by one user. Nothing is a benchmark. Each result is labeled **measured** or **estimated**.

## Findings that changed the design

| Finding | Status | Detail |
|---|---|---|
| Fetching must be throttled, but inventory can fan out | measured | In a 12-video batch the default fetch path produced 20 HTTP 429 errors, and 10 of 12 videos only succeeded through the subtitle API after about 35 s of failed retries each. Splitting fetch (bounded concurrency) from inventory (cache only, unlimited fan-out) removes the problem. |
| Subtitle API first is much faster per video | measured, 1 video | One uncached video fetched in 4.9 s with zero 429s, against about 37 s for the old path. The 12-video effect has not been re-run. |
| Cheap model is good enough for inventory | measured | Haiku: 1 of 558 items needed a quote fix (0.2%) in the batch, and 0 of 274 in two earlier two-video runs. All caches passed an independent `verify-quotes`. Recall (claims missed) was not measured. |
| Most inventory cost is fixed overhead | measured | Regressing each Haiku agent's tokens on its item count gave about 86.6K tokens fixed + 780 per item (R-squared 0.85, n=12). Roughly 70% of the 1.47M inventory tokens were fixed per-agent cost. What the fixed cost is made of is not known. |
| Raw claim count mis-ranks mixed batches | measured | A general feature roundup ranked first for "build websites with Claude". Adding a relevance factor (0.4 + 0.6 x relevance) fixed the order on that data and leaves rankings unchanged when no relevance is given (tested). |
| Fact-checking is worth its cost | measured | 21 points checked: 10 supported, 7 partly, 3 contradicted, 1 unverified. A search routing rule (regulator sites for legal claims, vendor docs for product claims) kept unverified results low. |

## Things that did not help

| Change | Result |
|---|---|
| De-overlapping rolling captions before inventory | Net token difference across two videos was 145 tokens (one video 11% lower, the other 12% higher). The subtitle API returns clean cues anyway, so on that path it only re-buckets text. |
| Moving the long inventory brief into a file | One video: 97,374 tokens against 94,321 with the long brief (about 3% higher; within noise). Launch prompts are about 80% shorter, so the benefit, if any, is orchestrator time. That time saving is not yet measured. |

## A bug the tests found

While writing tests for the de-overlap code, a case with two clean subtitle cues, "...zeta" then "eta theta iota", dropped the word "eta", because the overlap check matched the end of "zeta". Overlaps now have to start and end on word boundaries and span at least two words.

To size the damage I re-fetched four of the batch's transcripts and compared word counts. The old code had lost 4 of 6,032 words, 14 of about 19,500, 2 of 5,185, and 0 of 4,199, which is at most 0.1%. The inventories read those transcripts and their quotes verified against them, so the batch's conclusions are not affected, but the loss was silent. This is the argument for a test suite around even small helper scripts.

## Model and effort

One A/B with a single run per side: a Sonnet / medium-effort orchestrator against an Opus / high-effort baseline, both doing key-point merging, fact-checking and scoring on the same two videos. They produced the same ranking order and watch plan, found no contradicted core claims, and agreed on 5 of 7 shared verdicts. The Sonnet run caught caption garbles the Opus run missed and missed one regulator source that the Opus run found. The inputs differed (hand-built vs Haiku inventories), so scores are not comparable. Treat this as a reasonable default, not a result.

## Estimated, not yet tested

| Idea | Why it is only an estimate |
|---|---|
| Batch 2 to 3 short videos into one inventory agent: perhaps 35 to 45% fewer inventory tokens | Derived from the fixed-cost regression. Untested; it may cost latency or quality. |
| 12-video fetch phase of 20 to 30 s instead of 146 s | Extrapolated from a one-video test. |
| Shorter launch gap (162 s in the batch) | Prompts are shorter, but the run has not been repeated with stage marks. |
| Fact-check verdict cache | No cache hits yet; the first batch had none to reuse. |

## Telemetry design notes

- Subagents cannot time or count their own tokens. Early inventory events carried made-up `seconds` values (60, 60, 90, 120). The orchestrator now logs the harness's token count, tool calls and duration for each subagent, and subagents log only counts.
- Stage marks make wall-clock attributable. In the batch, about 40% of the 884 s ran between stages with nothing logged.
- Fact-check results are ingested from the agents' JSON, because hand-typed events had miscounted the searches (24 logged, 23 real).
- The telemetry sample in `examples/` contains harness numbers that were backfilled after the run, marked `backfilled=1`.

## A review that found the skill had lost its scripts

After the pipeline was built, a review of the telemetry and the live files turned up problems that the tests had not caught, because they sat between the pieces.

| Finding | Evidence | Fix |
|---|---|---|
| The live skill had been replaced by a single-file copy with no scripts | The skill folder's `SKILL.md` was rewritten at a later time, was 16.6 KB, and `scripts/` was gone. This is inferred to be an account-side sync; the repo copy was the only place the work survived. | Ship as a plugin so scripts travel with the skill; Step 0 refuses to run without them |
| The scorer silently dropped a mistyped point id | A one-character typo gave exit 0, no message, 2 of 3 points counted, and a plausible score (96.5 vs 97.4) | Validation: unknown ids are errors |
| Scoring input was hand-built and never saved | Key-point membership was typed by the orchestrator; the analysis lived in a scratch folder; two analyses of one video pair scored 90.9/87.8 and 80.9/79.2 and could not be compared | Persisted `keypoints.json` and `verdicts.json`, a builder that checks every link |
| Stage timings were not real | Five marks landed within 0.7 s after a 40 s gap, so one stage was credited with 99% of the run | Mark status plus a burst warning |
| No guard against instructions hidden in untrusted text | The subagent brief did not say to treat transcripts as data | Guard in both briefs, plus a phrase scan |
| A cleaner fix could not be re-applied | The cleaned transcript replaced the raw one; the earlier word-dropping bug was sized by re-fetching | Raw cues kept in a sidecar; cleaner versioned |

The scan found no instruction-like phrases in 14 real cached transcripts, so it has not been tested on a real attack, only on unit tests. The input builder has been tested on synthetic caches, not yet on a full real run.
