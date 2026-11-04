# Phase B inventory brief (youtube-video-ranker)

You are an inventory subagent. Your launch prompt gave you these values; use them wherever this brief says the name in capitals:

- `VIDEO_ID`: the 11-character YouTube ID to process
- `RUN_ID`: the telemetry run id
- `CLAIMS_DIR`: absolute path of the installed `youtube-claims` skill
- `RANKER_DIR`: absolute path of the `youtube-video-ranker` skill
- `MODEL_NAME`: the model name to record in telemetry (for example `haiku`)

Do the youtube-claims Pass 1 (structure) and Pass 2 (claim and evidence inventory) for ONE video from its already-cached transcript. Do not use the network. Stop after Pass 2.

## Environment (Windows plus Git Bash; you have Bash)

- Prefix every Python call with `PYTHONUTF8=1`. The scripts print check marks and box characters that crash a cp1252 console.
- Native Windows Python cannot open `/c/...` paths. Use `C:\...` paths, or convert with `cygpath -w`.
- The claims scripts are stdlib-only, so plain `python3` works. Use `python3 -I` for telemetry.

## Steps

1. Read `CLAIMS_DIR/prompts/extract_structure.md` and `CLAIMS_DIR/prompts/inventory_claims.md`. Follow their output shapes exactly.
2. Load `VIDEO_ID.json` from the cache directory `~/youtube-reports/.cache/` (expand `~` to the user's real home directory; on Windows give Python a native path). It holds `video_id`, `title`, `duration_seconds` and `transcript` as a list of `{start, duration, text}`. The Pass 1 and Pass 2 caches may not exist yet; if they do, you still regenerate them.
3. **Pass 1.** Split the transcript into contiguous, non-overlapping sections of type `hook`, `content`, `pitch` or `outro`, 3 to 11 of them. Write the cache. Build a wrapper `{"inputs":{"transcript":<the fetch json>},"output":<pass1>}` in a temp file, then run: `cat wrapper | PYTHONUTF8=1 python3 "CLAIMS_DIR/scripts/cache.py" write 1 VIDEO_ID "CLAIMS_DIR/prompts/extract_structure.md"`.
4. **Pass 2.** For each section, list `concrete_claims`, `vague_claims`, `evidence_shown` and `pitches`.
   - Every item needs a `timestamp` (`M:SS`, or `H:MM:SS` when the video is an hour or longer) and a `quote` that is a verbatim substring of ONE transcript segment's `text`. Never join segments.
   - Generic like or subscribe requests are not pitches. Selling a course, community, product or affiliate link is.
   - Every Pass 1 section id must appear in `by_section`.
   - Be selective: prefer specific, checkable claims over restating every sentence.
5. **Verify before writing.** `cat pass2.json | PYTHONUTF8=1 python3 "CLAIMS_DIR/scripts/cache.py" verify-quotes VIDEO_ID` must exit 0. Fix any mismatch by choosing a different verbatim substring, and count how many items you had to fix as `verify_quotes_fail`.
6. **Write the Pass 2 cache.** Wrapper `{"inputs":{"pass1":<pass1>,"transcript":<the fetch json>},"output":<pass2>}` piped to `cache.py write 2 VIDEO_ID "CLAIMS_DIR/prompts/inventory_claims.md"`.
7. **Telemetry.** Count `pass1_sections` and `pass2_items` (concrete plus vague plus evidence plus pitch items across all sections). Then log one event. Do NOT report seconds or tokens: you cannot measure them, and the orchestrator records the real values from the harness.

```
PYTHONUTF8=1 python3 -I "RANKER_DIR/scripts/telemetry.py" log --run-id RUN_ID \
  --stage inventory --event inventory --video VIDEO_ID --model MODEL_NAME \
  --kv ok=1 pass1_sections=N pass2_items=N verify_quotes_fail=N
```

## Reply

Reply with ONLY this JSON and nothing else, never the transcript or the inventory:

`{"video_id":"VIDEO_ID","pass1_sections":N,"pass2_items":N,"verify_quotes_fail":N,"status":"ok"}`

If something blocks you, set `status` to a short reason and still report the counts you reached.
