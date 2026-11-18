# Shared Kokoro engine for claude-voiceover: requirements

Version 1, 18 Nov 2026. Written against plugin 1.5.0 (commit 2658e91; the installed copy matches the checkout except for a marker file and one lock file). `<state>` means the plugin's state folder: `$VOICEOVER_DATA_DIR`, else `~/.claude-voiceover`.

## 1. Summary

**Goal.** Exactly one process on the machine holds the Kokoro model on the graphics card, and it serves both the plugin's narration and a second local program (a voice assistant) that needs the audio returned to it instead of played. The plugin provides that engine, a documented local interface to it, and a way to start it when no Claude Code session exists.

**Current state (measured on the target machine, 18 Nov 2026).**
- Engine: kokoro-onnx 0.6.1 on ONNX Runtime GPU 1.31.1, full-precision 325 MB model, 54 voices. One process per narrated project, up to two at once, plus a one-shot speaker that loads its own copy. Every mode plays on the speakers; none returns audio.
- Card: GTX 1650, 4096 MiB. Other programs hold 1149 MiB. One Kokoro process holds 584 MiB after loading and 1120 MiB after speaking: one copy leaves 1443 MiB free, a second about 320 MiB.
- Speed on the card, warm: a 15-word sentence (4.25 s of audio) in 0.45 s; first sound of a sentence-by-sentence stream after 0.21 s; model load 1.1 s. On the processor: 1.1 s, 0.35 s, 0.9 s.

**The MUSTs, by title.** M1 one model-holding process · M2 drainers and one-shot speaker use it · M3 an interface that returns audio · M4 audio format fixed and documented · M5 never on the speakers, never in a queue · M6 any voice, stated speed rule · M7 end marker and explicit errors · M8 text in fragments, flush · M9 cancel within 0.3 s, disconnect counts as cancel · M10 local-only, owner-only transport · M11 versioned protocol in INTERFACES.md · M12 fixed socket path and fixed launcher · M13 race-safe start · M14 survives a plugin update · M15 load on demand, unload when idle, card fully freed · M16 status and release requests · M17 sentence-level interleaving · M18 narration rules never touch interface requests · M19 memory budget · M20 processor fallback, reported · M21 no stale socket or lock, never a hang · M22 request limits · M23 narration stays fail-silent · M24 narration unchanged for the user · M25 macOS, no-model, dry-run and the standard-library hook layer keep working.

## 2. The second caller (not plugin work; stated so the plugin knows what it serves)

A voice assistant on the same machine speaks its replies as voice notes and in live calls. Its framework starts one local command, keeps it warm and talks to it over pipes. The framework's contract for that command says:

> stdin: one JSON request per line: `{"text": "...", "params": {...}}`
> stdout: big-endian uint32 length + payload, repeated. A ZERO-LENGTH frame ends one utterance.
> The process is kept WARM and shared. It is spawned once and reused across utterances, then retired after five idle minutes.
> Emit the encoding you declared. The sink refuses to play an encoding it does not recognise.
> [The provider must] accept text incrementally, return audio incrementally (early first bytes, then a continuous flow), support flush (synthesize what's buffered) and clear (barge-in cancel).
> Death is observed and surfaced; the engine is respawned on next use, and after three consecutive failures it degrades to honest silence.

The declared encoding is `linear16`, 24000 Hz, mono. That command will be a **thin client with no model**: it forwards English text to the plugin's engine and relays the audio. Its voice is `bf_emma` at speed 1.0 unless told otherwise.

Four facts from the framework's implementation bind the plugin side:
1. The framework buffers fragments itself and sends one request line per flush; requests are answered strictly in order.
2. **Its cancel is a kill.** On an interruption it sends SIGTERM to the command and starts a new one on next use. The engine will usually see a cancel as an abrupt disconnect, soon followed by a new connection.
3. It kills the command if one audio frame exceeds 4 MiB.
4. The command may run in a sandbox with limited access to the host (see Not verified).

## 3. What the code does today

| Topic | Finding | Where |
|---|---|---|
| Device choice | `ONNX_PROVIDER` in the environment wins; else machine setting `tts_device` = `gpu` selects CUDA after `onnxruntime.preload_dlls()`; else CPU. | `tts/kokoro_voice.py`: `choose_onnx_provider`, `_gpu_provider` |
| Model load | `_load_on` sets `ONNX_PROVIDER` and builds `Kokoro(MODEL_FILE, VOICES_FILE)`. `_load_accelerated` proves the card by synthesizing "Ready." and returns None on any error; `load_kokoro` then reloads on CPU. | same file |
| Session | Built inside the library as `InferenceSession(model_path, providers=[provider])`. **No session options and no provider options are set anywhere**: memory arena, convolution search and memory limit are ONNX Runtime defaults. The library also offers `Kokoro.from_session(session, voices_path)`. | `kokoro_onnx/session.py`: `create_session`; `kokoro_onnx/__init__.py` |
| Precision | `~/.kokoro-tts/models/kokoro-v1.0.onnx`, 325,532,387 bytes, full precision. | `kokoro_voice.py`: `MODEL_FILE` |
| Narration | `drain` → `_speak_item` splits with `split_sentences`; `KokoroSpeaker._render` runs `kokoro.create(sentence, voice, speed, lang, trim)` on a one-thread pool, one sentence ahead; `KokoroSpeaker.play` writes `<project>/play/<pid>-<n>.wav`; `play_file` starts `aplay`, `afplay` or `ffplay` (winsound on Windows). Output is float32 at 24000 Hz. | `tts/engine_common.py`, `kokoro_voice.py` |
| One-shot speaker | `speak_text` claims `<state>/tts.lock`, loads its own model; `stream_chunks` plays each chunk from a temp wav. | `kokoro_voice.py` |
| Voice and speed | Voice: explicit `voice` setting, else a hash of the project root over 13 ids; stored in the queue item. Speed: `VOICE_SETTINGS[voice]` (13 entries; `bf_emma` 1.0, `bm_daniel` 1.3), 1.0 for any other id. The library rejects an unknown voice and a speed outside 0.5–2.0. | `voiceover/settings.py`: `get_voice`; `kokoro_voice.py`: `get_voice_settings`; library `_prepare` |
| Which processes exist | `ensure_drainer` spawns `uv run --project <plugin>/tts[-gpu] …/kokoro_voice.py --drain <project dir>` when the queue has items or a lease is live. `drain` claims `<project>/tts.lock` (atomic; a dead owner counts as free), steps aside when `MAX_ENGINES` (2) other projects hold locks, stays while `lease_is_live`, exits when the queue is dry and no lease remains. Leases: 600 s (mod), 120 s (hooks). | `voiceover/speech.py`, `voiceover/lease.py`, `engine_common.py`: `drain`, `_other_engines_running` |
| Worst case today | Two drainers plus a one-shot speaker: three models, 3 × 1120 + 1149 = 4509 MiB against 4096 (derived from the code, not measured). | |
| Card full or missing | At load: CPU for that process's life, recorded only in the debug log. After load: a failed sentence skips the rest of its item; no reload on the processor. A failed `open_speaker` backs the hooks off 30 s. No status surface. | `_load_accelerated`, `drain` |
| Paths | The plugin root, and the `tts-gpu` environment in it, sit in a folder named after the version. | `speech.py`: `_PLUGIN_ROOT` |

## 4. Requirements

### One engine
- **M1.** At any moment at most one process on the machine holds the Kokoro model (on the card or the processor), whatever the number of narrated projects, one-shot calls and interface callers. *Why: card memory is per process; a second copy leaves about 320 MiB.*
- **M2.** Per-project drainers and the one-shot speaker (`--voice … [--stream]`, setup test, voice preview) MUST get their audio from that engine, or run inside it, and never load the model themselves. `MAX_ENGINES` may remain as a cap on projects audible at once; it MUST NOT mean two models. *Why: today's rules allow three models, 4509 MiB.*

### Interface that returns audio
- **M3.** The engine MUST accept from a local caller a request with text, a voice id, a speed and a caller-chosen request id, and stream the audio back as it is generated, sentence by sentence or finer. *Why: no current mode returns audio; first audio in 0.21 s instead of 1.5 s for a 64-word paragraph.*
- **M4.** Returned audio MUST be raw signed 16-bit **little-endian** PCM, 24000 Hz, mono, whole samples, no header. The byte order MUST be written into INTERFACES.md and announced in the handshake. *Why: the consumer declares `linear16` at 24000 Hz and refuses anything else; 24000 Hz is the model's own rate.*
- **M5.** Audio requested through the interface MUST never be played on the speakers and never enter any project's queue, play folder or lock. *Why: the caller owns playback; a private voice note must not sound on the laptop.*
- **M6.** Any voice id in the voice file MUST be accepted (54 here), not only the 13 in `VOICE_SETTINGS`. Without a speed, the plugin's per-voice speed applies, and 1.0 for a voice without one; a given speed is used as given, within 0.5–2.0. *Why: the assistant picks its own voice (`bf_emma`, 1.0); the library enforces that range.*
- **M7.** Every started request MUST end with exactly one end-of-utterance message stating `complete`, `cancelled` or `error`; an error MUST carry a code and a readable message and MUST NOT close the connection unless the stream is unusable. *Why: the consumer turns the end into its own terminator and counts failures.*

### Incremental text and control
- **M8.** For one utterance the engine MUST accept text in fragments, synthesize each sentence as soon as it is complete (the `split_sentences` rule), hold back a trailing partial sentence, and on `flush` speak what is buffered and acknowledge once that audio has been sent. *Why: replies arrive as they are written.*
- **M9.** A cancel MUST stop its request without affecting any other request or caller: no audio for it is sent after the cancel is read, its end message (`cancelled`) follows within 0.3 s, and no new sentence is started for it. A sentence already inside the model may finish and be discarded. A dropped connection MUST count as a cancel of all its requests. *Why: the consumer's interrupt is a kill; a running inference cannot be interrupted and lasts about 0.45 s for 15 words.*

### Transport and entry point
- **M10.** The interface MUST be reachable only from the same machine and by the same user; no listener on any network interface by default. *Why: it accepts arbitrary text and uses the card.*
- **M11.** The final wire format MUST be written into INTERFACES.md with an integer protocol version that the engine announces and a client can check; an unsupported version is refused with an explicit error. *Why: the two sides are released separately.*
- **M12.** The socket path and the command that starts the engine MUST be fixed, documented, free of the plugin's version number, and usable when no Claude Code session is running. *Why: the installed path changes on every update; the assistant speaks at any hour.*
- **M13.** Starting MUST be safe under a race: of any number of simultaneous starters exactly one engine survives and the others connect to it. *Why: narration hooks and the assistant start independently.*
- **M14.** A plugin update while the engine is loaded MUST NOT break a request in flight, change the socket or launcher path, or put two models on the card; the new version takes over no later than the old engine's next idle moment. *Why: both version folders exist side by side after an update.*

### Lifecycle and the card
- **M15.** The engine MUST load on demand; stay loaded while any narration lease is live, any request is in progress or any caller is connected; and, once none of these has held for a configurable idle time, unload and free the card completely (exiting is allowed and recommended). The next request reloads it without manual action. *Why: 1120 MiB parked on a 4 GB card blocks other work; reload costs 1.1 s plus 0.73 s for the first call.*
- **M16.** The engine MUST answer a `status` request (protocol and plugin version, loaded or not, card or processor with the reason for any fallback, card memory held, active callers, live leases, uptime, idle time) and a `release` request that frees the card at once and confirms it. Status MUST NOT load the model. *Why: another program, such as local speech-to-text (estimated 1.5–2 GB), fits only with no Kokoro on the card.*

### Sharing and fairness
- **M17.** Work from different callers MUST be interleaved at sentence granularity: a waiting caller's next sentence starts after at most one sentence from each other active caller. One inference runs at a time. *Why: nobody waits for another's whole message; at about 9× real time (4.25 s in 0.45 s) one engine has room for several listeners.*
- **M18.** The narrator's own rules (jump rule, cutover on the user's prompt, stop requests, sticky items, level and enabled settings, headless stand-down, the one-shot interrupt, the `tts_controller.py stop` kill switch) MUST apply only to narration. They never drop, silence or delay an interface request beyond M17, and never kill the engine while an interface request is in flight. *Why: today's kill patterns match `kokoro_voice.py` by name (`process_utils.TTS_PROCESS_PATTERNS`, `tts_controller.UNIX_PATTERNS`).*

### Memory
- **M19.** The one process MUST NOT hold more card memory than today's single engine, about 1120 MiB, whatever the number of callers, voices or queued requests. *Why: measured after speaking; it keeps 1443 MiB free.*

### Failure behaviour
- **M20.** When the card is full or unusable at load, **or a synthesis fails on the card later**, the engine MUST continue on the processor and say so in status, with the reason. *Why: today a mid-run failure only skips the item; the processor still gives 1.1 s for a 15-word sentence.*
- **M21.** A crashed or killed engine MUST leave nothing that blocks the next start (no stale socket, lock or marker needing cleanup). A caller always gets an error, an end message or end-of-stream, never a hang: a connected caller sees the engine's death within 1 s.
- **M22.** The engine MUST enforce and document limits: control line 64 KiB; text per utterance 6000 characters (today's `_FULL_TEXT_CAP`); audio frame 32,768 bytes; a bounded number of connections and queued requests; bounded buffering for a caller that stops reading, which is dropped after 30 s without stalling others. *Why: the consumer kills its command on a frame over 4 MiB; a stuck caller must not pin memory.*
- **M23.** Narration MUST stay fail-silent: no engine problem may raise an error in, or block, a Claude Code session; hooks still exit 0 and print nothing.

### Compatibility
- **M24.** For the user, narration MUST behave as in 1.5.0: per-project voices and speeds, jump rule, cutover, sticky alerts, chimes, levels, two projects audible at once, prefetch of the next sentence, first sound no later than today.
- **M25.** macOS `say` engines, the no-model case (`resolve_engine` → `none`), dry-run mode and `VOICEOVER_NO_SPAWN` MUST keep working, and `hooks/` and `voiceover/` gain no import outside the standard library. An interface request MUST NOT trigger a model download; it gets `no_model`.

### SHOULDs
- **S1.** Reduce the card footprint below 1120 MiB. Loading takes 584 MiB and speaking adds about 536 MiB, which points at the arena and convolution workspace, not the weights. **Untested suggestions**, each to be measured on the card: build the session in plugin code and pass it through `Kokoro.from_session`, with CUDA provider options `arena_extend_strategy=kSameAsRequested`, `cudnn_conv_algo_search=HEURISTIC` (or `DEFAULT`), `cudnn_conv_use_max_workspace=0`, and `gpu_mem_limit` as a hard ceiling; the run option that shrinks the card arena after each run; session options `enable_mem_pattern=False` and `enable_cpu_mem_arena=False` (host RAM); a half-precision model file.
- **S2.** Cap one unit of work: split sentences longer than about 40 words at clause boundaries. *Why: a 64-word single chunk held the model for 1.5 s, and long inputs may enlarge the arena.*
- **S3.** Latency targets, warm, engine otherwise idle: first audio of a 2-word sentence within 0.35 s (baseline 0.21 s); a 15-word sentence complete within 0.7 s (baseline 0.45 s). Cold, from the start command to first audio: within 3 s (the plugin's own figure is about 2.1 s).
- **S4.** `release` SHOULD accept "keep off the card for N seconds", during which any start uses the processor. *Why: otherwise the next narrated sentence takes the card back.*
- **S5.** A connected caller silent for longer than a configurable cap (suggested 10 minutes) SHOULD stop counting toward M15.
- **S6.** The launcher SHOULD resolve `uv` by absolute path and return only once the socket accepts. *Why: a caller's environment may have a minimal PATH.*
- **S7.** The engine SHOULD import everything it needs before serving. *Why: the old version's folder may be removed under a running engine.*

## 5. Proposed wire format (protocol 1)

A proposal. The plugin side may change it, provided the final format is in INTERFACES.md with its protocol version (M11).

- **Socket:** Unix stream socket `<state>/engine/engine.sock`; folder mode 0700, socket 0600. *Why a socket and not loopback HTTP: no network exposure, access enforced by the file system, no port to clash (port 8737 on this machine is already taken by another local service), nothing beyond the standard library.*
- **Single-instance lock:** an advisory lock on `<state>/engine/engine.lock`, held for the engine's lifetime. The kernel drops it when the process dies, so it cannot go stale; its holder may unlink a leftover socket file before binding.
- **Launcher:** `<state>/bin/voiceover-engine` with `ensure` (start if needed; return 0 once the socket accepts), `status`, `release`, `stop`. Standard library only. It finds the current plugin version through a pointer the plugin refreshes when its hooks run.
- **Caller → engine:** UTF-8 JSON, one object per line.
- **Engine → caller:** UTF-8 JSON, one object per line. A line with `"ev":"audio"` is followed by exactly `bytes` bytes of PCM, then the next line.

```
C  {"op":"hello","proto":1,"client":"assistant-voice"}
E  {"ev":"hello","proto":1,"plugin":"1.6.0","audio":{"format":"pcm_s16le","rate":24000,"channels":1},
    "limits":{"line":65536,"text":6000,"frame":32768}}
C  {"op":"start","id":"u1","voice":"bf_emma","speed":1.0}
C  {"op":"text","id":"u1","text":"The build passed. I am now run"}
E  {"ev":"audio","id":"u1","seq":0,"bytes":32768}      + 32768 bytes
E  {"ev":"audio","id":"u1","seq":1,"bytes":21504}      + 21504 bytes
C  {"op":"text","id":"u1","text":"ning the tests"}
C  {"op":"flush","id":"u1","n":1}
E  {"ev":"audio","id":"u1","seq":2,"bytes":32768}      + 32768 bytes
E  {"ev":"audio","id":"u1","seq":3,"bytes":28672}      + 28672 bytes
E  {"ev":"flushed","id":"u1","n":1}
C  {"op":"end","id":"u1"}
E  {"ev":"end","id":"u1","reason":"complete","samples":57856}

C  {"op":"start","id":"u2","voice":"bf_emma","text":"Hello there.","end":true}     whole utterance in one line
C  {"op":"cancel","id":"u2"}
E  {"ev":"end","id":"u2","reason":"cancelled","samples":0}

E  {"ev":"error","id":"u3","code":"unknown_voice","message":"voice zz_top is not in the voice file"}
E  {"ev":"end","id":"u3","reason":"error","samples":0}

C  {"op":"status"}
E  {"ev":"status","proto":1,"plugin":"1.6.0","pid":4242,"loaded":true,"device":"cuda","requested":"cuda",
    "fallback":null,"vram_mib":1120,"callers":2,"requests":1,"leases":1,"uptime_s":312,"idle_s":0,"idle_unload_s":60}
C  {"op":"release","keep_off_s":600}
E  {"ev":"released","vram_mib":0}
```

Rules. `hello` comes first. Requests on one connection are served in the order started (a caller may send the next one early, which gives narration its prefetch); different connections interleave (M17). `seq` counts a request's frames from 0; `bytes` is even and at most `limits.frame`; `samples` is the total sent, so total bytes = 2 × `samples`. `speed` and `lang` are optional (M6). Unknown fields are ignored; an unknown `op` gets `bad_request`. `cancel` without `id` cancels everything on the connection. Error codes: `bad_request`, `unsupported_protocol`, `unknown_voice`, `bad_speed`, `text_too_long`, `busy`, `no_model`, `synthesis_failed`, `released`, `restarting`.

Mapping on the caller's side, for information: one consumer request line becomes `start` with `text` and `"end":true`; each `audio` payload becomes one length-prefixed frame; `end` becomes the zero-length frame.

## 6. Acceptance tests

"Card list" is the card's per-process memory listing.

| # | Scenario | Pass when |
|---|---|---|
| T1 | Narrator level speaking in two projects at once, plus an interface request (`bf_emma`). | The card list shows exactly one plugin process, holding ≤ 1120 MiB (+5%); both projects are heard; the caller receives its audio. |
| T2 | Interface request "Hello there." | Handshake announces `pcm_s16le`, 24000, 1; total bytes even and equal to 2 × `samples`; wrapped in a WAV header with those values the audio is clear speech at normal pitch, and noise when byte-swapped; exactly one `end`/`complete`; no player process started; no new file in any project's queue or play folder. |
| T3 | Each of the 54 voice ids, then an unknown id. | Audio for all 54; `unknown_voice` then `end`/`error` for the last; the connection stays usable. |
| T4 | Text in fragments that split a word; then flush. | The first sentence's audio arrives before the second sentence's text is sent; the trailing fragment stays silent until flush; `flushed` follows its audio. |
| T5 | Cancel a 60-word utterance after its first frame while a project narrates and another caller speaks. Repeat, killing the caller instead. | No frame for that id after the cancel is read; `end`/`cancelled` within 0.3 s; the others continue with no gap longer than one sentence; after the kill, a reconnect gets first audio of a short sentence within 0.5 s from the same engine process. |
| T6 | Warm engine, alone; then with two projects narrating long messages. | Alone: S3 met. Busy: first audio of a short sentence within S3 plus two sentence times (about 1.2 s). |
| T7 | During an interface request: the user's prompt in a narrated session, a stop request, level set to silent, the one-shot interrupt, the kill switch. | The interface stream completes intact each time; narration reacts as in 1.5.0. |
| T8 | Idle time set to 10 s; stop all activity; later send a request. | Within idle time + 5 s the card list shows no plugin process and used memory is back at the baseline (1149 MiB here); the request succeeds through the documented start path, first audio within 3 s. |
| T9 | Engine killed (SIGKILL) mid-request. | The caller sees end-of-stream within 1 s; no hook error in any session; the next start succeeds within 3 s with no cleanup; narration resumes with the next queued sentence. |
| T10 | Ten simultaneous `ensure` calls with nothing running. | Exactly one engine process; all ten return 0 and can connect. |
| T11 | Install a newer plugin version while the engine is speaking; remove the old version's folder. | The request in flight completes; socket and launcher paths unchanged; status shows the new version after the next idle moment; the card list never shows two plugin processes. |
| T12 | Start with the card full (under 500 MiB free) or hidden; separately, make a synthesis fail on the card mid-run. | The engine serves from the processor; status shows `cpu` and the reason; a 15-word sentence within 1.5 s (baseline 1.1 s); narration works. |
| T13 | No Claude Code process on the machine and no lease; run the launcher from a plain shell and send a request. | Audio is returned; the engine unloads after the idle time. |
| T14 | `release` during narration, with a keep-off time. | Within 2 s the card list shows no plugin process; `released` is received; narration continues on the processor and returns to the card after the keep-off time. |
| T15 | 6001 characters of text; a 70 KiB line; a caller that stops reading. | `text_too_long`; `bad_request`; other callers unaffected, the stuck one dropped after 30 s. |
| T16 | Existing suite (`tests/run_all.sh`), dry-run, macOS `say`, no model files. | Suite passes; dry-run prints as today; `say` paths untouched; the interface answers `no_model` without downloading; `hooks/` and `voiceover/` have no new import outside the standard library. |

## 7. Out of scope for the plugin

Hebrew and any other language Kokoro does not speak (the caller sends those sentences to Microsoft Edge read-aloud; they never reach the plugin); the assistant's thin client and its re-framing of the audio; the assistant's fallback to Edge when the engine cannot be reached; speech-to-text (the plugin only offers `status` and `release` to it).

## 8. Open decisions for the plugin's owner

1. **Socket or loopback HTTP.** Recommend the Unix socket: nothing on the network, no port clash, standard library only. HTTP helps only if a caller cannot reach a socket path.
2. **Drainers as clients of the engine, or hosted inside it.** Recommend clients: `engine_common.drain` and every lock, lease, jump and stop rule stay as they are and only the speaker's render call changes; narration then exercises the interface the assistant uses, and M18 holds by construction.
3. **Default idle time before unloading.** Recommend 60 s, configurable. Today narration unloads the moment a turn ends; 60 s bridges the gap between turns (a cold start costs about 2 s) without parking 1.1 GB.
4. **Priority when both wait.** Recommend plain round-robin by sentence, with an optional `live` flag that goes first among waiters: a live call's listener hears every delay, narration has a prefetched sentence in hand.
5. **Unload by exiting or in place.** Recommend exiting: the only way the card is certainly freed; reload is 1.1 s.
6. **Does the kill switch stop interface requests.** Recommend no: it stops narration only; add an explicit "all" form.
7. **On `release`, what running callers get.** Recommend a restart on the processor for the keep-off time, so narration and the assistant keep talking at processor speed.
8. **Windows.** Recommend the shared engine on Linux and macOS first, and today's behaviour on Windows until a loopback transport is chosen.

## 9. Not verified

- **Estimates:** speech-to-text at 1.5–2 GB; the three-model worst case of 4509 MiB (arithmetic from the code); "about 9× real time" (from one sentence) and room for several listeners; the 1.2 s busy-case target; the 3 s cold-start target (the 2.1 s baseline is the plugin README's figure, not re-measured).
- **Memory:** 1120 MiB was measured after 15- and 64-word inputs with a few voices; longer inputs, all 54 voices and several callers were not measured. Every option in S1 is untested and its name must be checked against ONNX Runtime 1.31.1. That the 536 MiB growth is arena and workspace is an inference. Whether a half-precision model file exists for this release and runs well on this card is unknown. Whether deleting the session frees the card inside a living process is unknown (hence decision 5). Processor RAM use was not measured.
- **Byte order:** the consumer's contract says `linear16` without naming a byte order; little-endian is assumed as the usual meaning.
- **Sandbox:** whether the consumer's command, if sandboxed, can reach a socket under the plugin's state folder or run the launcher was not tested; it is consumer-side work but may reopen decision 1.
- **Cancel:** that a running inference cannot be interrupted is taken from the library's own note on `create_stream`, not tested.
- **Updates:** how and when Claude Code removes an old version folder, and whether the `tts-gpu` environment is rebuilt for each version, were not checked.
- **Windows:** whether the plugin's Python on Windows has Unix sockets was not checked.
- **Not opened:** `voiceover/narrator.py`, the individual hook scripts, the mod's TypeScript files and the tests; their behaviour is taken from INTERFACES.md. The 54-voice count and the package versions come from the measurement record and the installed package folders, not from opening the voice file or the lock file.
- **Not run:** nothing was executed against the engine for this spec; timings and memory figures are those of the 18 Nov 2026 measurement. The card's 1149 MiB baseline and the use of port 8737 were re-read today and match.
