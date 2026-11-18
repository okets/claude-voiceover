# Shared Kokoro engine — on-card acceptance (Task 15)

19 Nov 2026, the owner's machine (GTX 1650, 4096 MiB; tts_device gpu). Branch build 323e714 installed locally as plugin 1.6.0 beside 1.5.0 (the plugin registry was pointed at 1.6.0; this Claude Code session kept narrating from 1.5.0, so one 1.5.0 drainer, holding about 610 MiB, was on the card throughout). Sharing on: `setup_tts.py --share-engine 47320` installed the systemd user socket. Tools: `EngineConnection` scripts and raw TCP, `nvidia-smi` per-process listing.

| # | Scenario | Result | Pass |
|---|---|---|---|
| Setup | `--share-engine 47320` | Socket unit active on `engine.sock` and `127.0.0.1:47320` only; engine dir 0700, socket and token 0600; token not printed in the transcript; unit ExecStart `/usr/bin/python3 <state>/bin/voiceover-engine serve` | ✅ |
| S3 cold | Connect after idle (socket activation) → first audio of "Hello there." | hello 0.56 s, first audio 2.17 s total (target ≤ 3 s; 1.5.0 README figure 2.1 s) | ✅ |
| S3 warm | "Okay." first audio, 5 runs | 0.14 s (0.22 s first run) — target ≤ 0.35 s, baseline 0.21 s | ✅ |
| S3 warm | 15-word sentence complete, 5 runs | 0.45–0.47 s — target ≤ 0.7 s, baseline 0.45 s | ✅ |
| M19 | Engine card memory after speaking | 1104 MiB (budget ≤ 1176 MiB) | ✅ |
| T1 | Two projects narrating plus a live interface request (`bf_emma`) | One 1.6.0 process on the card in every sample; interface first audio 0.59 s while narration ran. Project A was heard; project B was held by the existing two-projects cap (this session's 1.5.0 narration + A held both slots, as in 1.5.0) and was spoken from the same engine once A finished | ✅ |
| T2 | "Hello there." | hello announces pcm_s16le / 24000 / 1; bytes even (49152); WAV written; heard by the owner during T1 | ✅ |
| T3 | All 54 voices, then `zz_top` | 54/54 complete; `unknown_voice` then the connection still completes a request | ✅ |
| T8/T19 | `engine_idle_s` 10, connection left open and idle | Engine gone from the card 10.5 s after the last request; used memory back to baseline (1756 vs 1754 MiB); next request first audio 1.86 s from connect | ✅ |
| T9 | SIGKILL mid-request | Client saw `disconnected` in 0.10 s; new engine (new pid) first audio 1.89 s after reconnect | ✅ |
| T11 | Engine running from a 1.6.1 copy; 1.6.2 copy prepared and the 1.6.1 folder deleted mid-request | In-flight request completed (880640 bytes); old engine exited 6.2 s after the request; next request ran on 1.6.2; socket and launcher paths unchanged; never two plugin processes on the card | ✅ |
| T14 | `release` with keep-off 40 s | Card freed in 0.26 s; next request served on CPU with status `fallback.reason = keep_off`; after keep-off, the engine was back on cuda | ✅ |
| T12 CPU speed | 15-word sentence on CPU | 1.60–1.75 s (target ≤ 1.5 s). Not a regression: 1.5.0's own engine on the same machine at the same moment took 1.57–1.72 s — the machine was slower than on 18 Nov (1.1 s baseline) | ⚠️ machine-bound |
| T20 | Client killed mid-utterance, reconnect, "Okay." | 0.53 s with the dead client's sentence still rendering (allowed ≈ 0.7 s); 0.14 s once it cleared | ✅ |
| TCP | Token + hello + live start in one write | hello 2.4 ms; first audio 0.34 s | ✅ |
| Narration cold | Drainer spawn + engine cold start + first sentence | First sound 2.45 s after queueing, from fully cold (the prompt-time prewarm normally hides this) | ✅ |

Observations:
- After an adopted engine has run, `ss` shows the systemd-held TCP socket's backlog as 100 (asyncio re-calls `listen(100)` on the adopted socket). Harmless.
- The engine's status `vram_mib` lags `nvidia-smi` by up to one 5 s timer tick.

Not run here: T5 cancel inside the box on the card, and the boxed latency figures — handed to the my_agent session's probe (results appended below when received). T6 (busy, two long narrations) is partly covered by T1's 0.59 s.
