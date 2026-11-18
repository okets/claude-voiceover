# Shared Kokoro engine: design

18 Nov 2026. Builds on plugin 1.5.0. Requirements: [2026-11-18-kokoro-shared-engine-requirements.md](2026-11-18-kokoro-shared-engine-requirements.md) (M1–M25, S1–S7, T1–T16 below refer to it). `<state>` is the plugin state folder (`$VOICEOVER_DATA_DIR`, else `~/.claude-voiceover`).

## 1. Intent

One process on the machine holds the Kokoro model. It narrates Claude Code sessions as today, and it streams raw audio to the owner's voice assistant (the "my_agent" framework) for live calls, voice notes and read-aloud. The 4 GB card must never hold two copies of the model.

The owner's overriding requirement: **neither the plugin nor the agent voice may feel laggier than today.**

### Goals
- M1–M25 of the requirements, with the changes in section 3.
- Narration first sound no later than 1.5.0, ideally earlier (drainers no longer load a model).
- Agent: warm first audio ≤ 0.35 s; first audio ≤ 0.5 s after the `start` line on a fresh connection to a loaded engine (the post-interrupt path, the agent's most latency-critical one); a cold start never heard on a live call.

### Non-goals
- The assistant's thin client, its CONNECT tunnelling and re-framing, its fallback to Edge (agent side).
- Hebrew or any non-Kokoro language.
- Windows: keeps 1.5.0 behaviour (each drainer loads its own model) until a Windows transport is chosen.
- macOS launchd socket activation (see 5.9).

### Anti-goals (a failure even if it "works")
- A Claude Code session that blocks, errors or prints because the engine is down or slow (M23).
- The user's stop key, cutover or kill switch silencing the assistant mid-sentence (M18).
- A cold start heard on a live call.
- Anything listening on a network interface without the owner turning it on.

## 2. Constraints learned from the caller (my_agent session, read from its framework code)

- Its client runs in a bubblewrap box: own network and pid namespace, home unreadable, no host Unix sockets, PATH without `uv`. It **cannot reach a Unix socket under `<state>`** and **cannot start the engine**.
- Its only way out is the box's HTTP proxy, which allows exactly `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>` for one owner-confirmed port. The client tunnels raw TCP with HTTP CONNECT, so the line+binary protocol runs unchanged. It dials `127.0.0.1`. Ports 4321 and 8737 are taken on this machine.
- Secrets reach the client only as environment variables from the assistant's own secrets file. So the token lives in our 0600 file and the owner copies it once.
- One client process serves everything (live call, voice note, read-aloud, fillers): **one connection, strictly sequential requests**, no parallel ones from the agent.
- Live call: client spawns at call connect, before the user speaks; text arrives sentence by sentence (first at once, then coalesced to about 100 characters per request). Voice notes and read-aloud: whole text in one request; give-up times 12–30 s.
- Interrupt = SIGTERM of the client, then a new client on the next sentence → abrupt disconnect then a fresh connection, token, hello, start. **Keep that path cheap.**
- The client stays alive 5 idle minutes and may hold its connection open the whole time.
- It kills its client on a frame over 4 MiB. Linear16 is little-endian on its side (confirmed).
- **The probe has already run, against the agent's own stand-in listener inside the real box.** Results:
  - **Proxy.** CONNECT through the proxy works, using Basic proxy auth. A direct connect is refused. An unconfirmed port gets a 403.
  - **Streaming.** Frames are streamed with no buffering in between.
  - **Timing.** These are medians over 5 fresh boxes. Box set-up takes 241 ms. The tunnel is up at 248 ms, `hello` arrives at 291 ms, and the first frame at 333 ms. About 0.3 s of every post-interrupt answer is therefore on the agent's side.
  - **Cancel.** After SIGTERM, EOF reaches the engine within about 5 ms.
  - **Client.** The client is `/usr/bin/python3` 3.14 using only the standard library, so it can't share code with the plugin.
  - **Next test.** The agent will repeat the test against the real listener, using a token kept in a scratch file.

## 3. Decisions

Settled with the owner:

| # | Decision |
|---|---|
| D1 | **Transport:** both a Unix socket (narration clients; owner-only by file mode, no token) and a `127.0.0.1` TCP listener (the assistant; token required). Same protocol on both. TCP is **off by default**; turned on by setting a port (M10). |
| D2 | **Start:** systemd **user socket activation** on Linux. The unit listens on both addresses and starts the engine on the first connection. Narration never depends on it: if the socket does not answer, the drainer starts the engine itself through the launcher. |
| D3 | **Drainers are clients** of the engine (requirements decision 2). Every jump/stop/lease/cutover rule stays in the drainer, so M18 holds by construction. |
| D4 | **Idle unload default: 330 s when sharing is on** (an `engine_port` is set; just over the agent client's 5-minute window), **60 s otherwise**: narration-only machines don't park the model, and the prompt-time prewarm hides the cold start. The `engine_idle_s` setting overrides both. Only requests queued or in progress, and `warm` holds, keep the model loaded. **An idle open connection does not.** This replaces the M15 clause "or any caller is connected" and makes S5 unnecessary. |
| D5 | **`warm` op:** loads the model if needed and restarts the idle clock; optional `hold_s`. The agent sends it the moment its client starts (call connect), so the cold start overlaps call setup. |
| D6 | **`live` flag** on `start`: first among waiters at the next sentence boundary. Never preempts a sentence in the model, never starves narration beyond one sentence per turn. The agent will send it once its framework change lands. |
| D7 | **Clause split:** sentences over ~40 words split at clause boundaries (S2). |
| D8 | **Unload by exit** (requirements decision 5). `release` restarts the engine on the processor for the keep-off time (decision 7). |
| D9 | **Kill switch stops narration only;** `stop --all` (new) also stops the engine (decision 6). |
| D10 | **Round-robin by sentence** across connections (decision 4). |
| D11 | Linux and macOS get the shared engine; Windows unchanged (decision 8). |
| D12 | Frames: first frame sent as soon as the first sentence is rendered; never wait to fill 32 KiB. |

Cost accepted by the owner, on a machine with sharing on: on a talkative day the model stays on the card most of the time (about 1.1 GB); speech-to-text frees it at once with `release`.

## 4. Approaches considered

**Transport.** (a) Unix socket only — rejected: the assistant's box cannot reach it. (b) Loopback TCP only — works for everyone, but narration would then need the token and loses file-mode access control for no gain. (c) **Both, behind one listener seam — picked.**

**Starting the engine.** (a) **systemd socket activation — picked:** fixed addresses that survive updates (M12, M14), race-free start (M13), unload-by-exit with systemd still listening (M15), no stale socket (M21), very little code. (b) Always-on service that unloads the model in place — rejected: freeing the card inside a living process is unverified, and a resident process gains nothing over (a). (c) Manual or hook-started only — rejected: the assistant would fall back to Edge whenever no Claude Code session had started the engine.

**Narration inside the engine.** (a) **Drainers as clients — picked:** the drainer keeps every narration rule; only its speaker's render call changes. (b) Hosting the drain loop inside the engine — rejected: moves lock/lease/jump/stop logic into a long-lived shared process, where a narration bug or kill could hit the assistant (M18).

## 5. Design

### 5.1 Components

| Piece | Where | Environment | Role |
|---|---|---|---|
| Engine | `tts/kokoro_engine.py` (new; name must NOT match the kill patterns `kokoro_voice.py` in `process_utils.TTS_PROCESS_PATTERNS` and `tts_controller.UNIX_PATTERNS`) | `tts` / `tts-gpu` uv project | Holds the model, serves the protocol, schedules sentences. |
| Engine client | `tts/engine_client.py` (new) | stdlib only | Connect (Unix socket), hello, ensure-start, requests, frame reading. Used by the drainer speaker, the one-shot speaker and the launcher. |
| Drainer | `tts/kokoro_voice.py --drain` (kept, same name so kill patterns still stop narration) | **plain `python3`**, stdlib only on Linux/macOS | `drain()` unchanged; the speaker becomes `EngineSpeaker`. |
| One-shot / preview / setup test | `kokoro_voice.py --voice … [--stream]` | plain `python3` | Client of the engine; plays chunks as they arrive. |
| Launcher | `<state>/bin/voiceover-engine` | stdlib only | `ensure`, `serve`, `prepare <root>`, `status`, `release [--keep-off S]`, `stop`, `enable`, `disable`, `token`, `rotate-token`, `install-service`, `uninstall-service`. |
| Pointer | `<state>/engine/current.json` | — | `{"plugin_root", "plugin_version", "uv"}`; refreshed by the hooks. |
| systemd units | `~/.config/systemd/user/voiceover-engine.socket` + `.service` | — | Installed by `/voiceover:setup`; listen on the socket and (if a port is set) on `127.0.0.1:<port>`. |

`hooks/` and `voiceover/` gain no non-stdlib import (M25). The drainer and one-shot speaker drop the kokoro/soundfile imports on Linux/macOS: returned PCM is written to WAV with the stdlib `wave` module. On Windows the 1.5.0 in-process `KokoroSpeaker` path and `uv run` spawn stay as they are.

`voiceover/speech.py` chooses the drainer command by platform: `python3 <plugin>/tts/kokoro_voice.py --drain <dir>` on Linux/macOS, today's `uv run …` on Windows. `kokoro_command` (setup test, voice preview, one-shot) follows the same rule.

### 5.2 Files under `<state>/engine/` (folder mode 0700)

- `engine.sock` — Unix socket, mode 0600.
- `engine.lock` — advisory `flock` held for the engine's lifetime (single instance when not socket-activated; harmless when it is). The kernel drops it on death, so it never goes stale; its holder may unlink a leftover `engine.sock` before binding.
- `token` — 32 random bytes as hex, mode 0600. Created once (by setup or the first engine start), stable across restarts and updates; replaced only by `rotate-token`.
- `current.json` — the pointer (5.1).
- `keep_off.json` — `{"until": unix_s, "reason"}` written by `release`.
- `start.lock` — taken by `ensure` around a spawn so ten simultaneous `ensure` calls start one engine (M13, T10).

### 5.3 Starting

**Socket-activated (Linux, unit installed).** `voiceover-engine.socket` has `ListenStream=<state>/engine/engine.sock` (`SocketMode=0600`, `DirectoryMode=0700`) and, when the `engine_port` setting is set, `ListenStream=127.0.0.1:<port>`; `Accept=no`. The service's `ExecStart` is `<state>/bin/voiceover-engine serve`. The engine takes the listening sockets from `LISTEN_FDS`/`LISTEN_PID` (stdlib: fds start at 3) and identifies each by `getsockname`. Bytes a client sends while the engine loads wait in the backlog; the hello reply is simply late. When the engine exits idle, systemd keeps listening and the next connection starts it again.

**Self-started (no unit, macOS, or the unit is broken).** `voiceover-engine ensure`: try to connect; if that fails, take `start.lock`, retry the connect, else spawn `serve` detached (its own session, stdio to the log), then poll the connect until it answers or 10 s pass (S6). `serve` without `LISTEN_FDS` takes `engine.lock` (non-blocking; on failure another engine is up, exit 0), binds the Unix socket, and binds TCP if a port is set.

**Launcher → plugin.** `serve` reads `current.json` and execs `<python> <plugin_root>/tts/kokoro_engine.py`. Here `<python>` is the environment's interpreter, which `prepare` records after `uv sync`; `prepare` resolves `uv` by absolute path (S6). *(Changed during planning: `uv run` starts the engine as a child process, so systemd's `LISTEN_PID` would no longer match. Exec keeps the pid, and skipping uv at start also saves time.)* The hooks refresh `current.json` (atomic replace) and re-copy the launcher when its content differs, at most once a minute, from the session-start path; this is how a plugin update reaches the launcher (M12, M14). The launcher file itself carries no version.

**Who calls `ensure`.** The drainer's `open_speaker()` (replaces model loading), the one-shot speaker, and the shell. The prewarm at prompt submit already spawns the drainer, which now ensures the engine and sends `warm` — so narration's cold start stays hidden as today.

### 5.4 Protocol 1

Based on requirements section 5, with these additions. The final text goes into INTERFACES.md (M11).

- **Auth (TCP only):** the first line must be `{"op":"auth","token":"<hex>"}`. A wrong or missing token → `{"ev":"error","code":"unauthorized","message":…}` and close. Compare in constant time. The Unix socket skips this line (an `auth` line there is accepted and ignored).
- **hello** as in the requirements; must be cheap: no model load, no file reads beyond what is cached. Its reply adds `"loaded"` and `"device"`.
- **warm:** `{"op":"warm","hold_s":30}` (`hold_s` optional, default 0, max 600). Starts loading if not loaded, restarts the idle clock, and keeps the engine loaded until `now + max(hold_s, idle_unload_s)`. Reply `{"ev":"warm","loaded":bool,"device":…}` sent once loading has finished (the caller need not wait for it).
- **start:** adds optional `"live": true` (D6). `speed` and `lang` optional (M6); voice from the voice file (all 54).
- **stop** (launcher only): `{"op":"stop"}` → engine finishes nothing further, ends every request with `cancelled`, exits.
- New error code `unauthorized`. Everything else (text, flush, end, cancel, status, release, frame rules, limits, codes) as in the requirements.

`status` adds `"warm_until"` and `"idle_unload_s"`, and reports `leases` as the number of active warm holds (the engine reads no lease files; see 5.6).

### 5.5 Engine internals

- **Threads:** one asyncio loop (or selector loop) for all sockets; **one inference worker thread** (one inference at a time, M17). Audio is produced per sentence with `kokoro.create` (not `create_stream`), converted float32 → int16 little-endian with clipping, and cut into frames of ≤ 32,768 bytes; the first frame goes out the moment its sentence is done (D12).
- **Pipelined input, no write delay.** The agent sends its token, `hello` and `warm`/`start` in one write. The reader therefore handles every complete line already in its buffer, never assuming one line per read, and it parses past the auth line in the same pass. Every accepted TCP socket gets `TCP_NODELAY`, so small JSON lines and the first audio frame go out at once.
- **Requests:** each connection has a FIFO of requests (served in the order started). Each request has a text buffer; complete sentences (the `split_sentences` rule, then D7's clause split) become sentence jobs; a trailing partial waits for more text, `flush` or `end`.
- **Scheduler:** the worker picks the next sentence job by round-robin over connections that have one ready; among those, connections whose head request is `live` go first. At most one sentence per connection per turn. Narration prefetch = a drainer sending the next request early on its own connection; it is rendered in that connection's turn.
- **Early first clause for `live` requests** *(builder-check call)*. With one model call per sentence, first audio waits for the whole first sentence to render. A live call's first chunk can be about 100 characters, roughly 0.5 s of render time. So for a request flagged `live`, the first sentence is split at its first clause boundary after at least 4 words (a comma, semicolon, colon or dash), when one exists. Later sentences follow D7 as usual. Patching kokoro-onnx to abort a running inference is **not** planned. A cancelled sentence already in the model finishes (at most one sentence, M9), and the agent covers 0.5 s of silence with a filler clip.
- **Cancel (M9):** marks the request cancelled; queued sentence jobs are dropped; a sentence inside the model finishes and is discarded; `end`/`cancelled` is written at once (well within 0.3 s). A disconnect cancels all the connection's requests.
- **Slow readers (M22):** per-connection output buffer bounded (suggested 2 MiB, about 40 s of audio); a connection that keeps it full for 30 s is dropped. Writes never block the loop or the worker.
- **Limits (M22):** line 64 KiB, text 6000 characters per utterance, frame 32,768 bytes, at most 16 connections and 64 queued requests per connection (builder may tune; documented in INTERFACES.md).
- **Imports up front (S7):** everything, including numpy and onnxruntime, is imported before the first accept.

### 5.6 Lifecycle and idle unload (D4, D5, D8)

The engine stays loaded while any of these hold: a request is queued or in progress; a `warm` hold has not expired; less than `idle_unload_s` has passed (setting `engine_idle_s`; by default 330 s with an `engine_port` set, else 60 s, per D4) since the last request or `warm` ended. When none holds, it stops accepting, ends anything still open with `restarting` (normally nothing), and exits — freeing the card completely. Under socket activation systemd keeps listening; otherwise the next `ensure` restarts it.

**Narration leases.** The engine does not read lease files. A drainer that waits on a live lease (the existing `lease_is_live` branch of `drain()`) sends `warm` with `hold_s` ≈ 60 every 30 s. So the "stay loaded while a narration lease is live" rule of M15 holds, and the idle clock lives in one place.

**Voluntary exits other than idle unload** (a plugin update, 5.10; a `tts_device` change; returning to the card after keep-off, 5.7) happen only at a **quiet moment**: no request queued or in progress, no unexpired `warm` hold, **and no connection open on either listener** — or else at the normal idle-unload exit, whichever comes first. The assistant keeps its connection open for the whole call, so the engine never swaps under a live call; a drainer holds its connection only while it drains. *(Builder-check call: "no request in progress" alone would let the engine exit between two sentences of a live reply.)*

### 5.7 Device, fallback and release (M20, M16, S4)

- Device choice as today (`ONNX_PROVIDER`, else `tts_device`), loaded through the existing `load_kokoro` logic moved into the engine. A keep-off marker that has not expired forces CPU.
- **Mid-run card failure:** a synthesis exception on CUDA → reload on CPU in the same process, retry that sentence once, record `fallback: {"reason", "at"}` in status. (A CUDA session left behind may still hold memory; acceptable for a failure path, and the next idle exit frees it.)
- **release `{keep_off_s}`:** write `keep_off.json`, reply `released` (with `vram_mib: 0` claimed only after exit — so the reply is sent, then every open request is ended with `error`/`restarting`, then the process exits). Clients reconnect: the drainer's `EngineSpeaker` retries once on `restarting`, which starts a CPU engine; the agent's next request reconnects on its own. Without keep-off, release simply exits.
- **Back to the card:** a CPU engine whose keep-off has expired and that is not on CPU for a fallback reason exits at the next quiet moment (5.6); the next connection starts on the card.

### 5.8 Narration side

- **`EngineSpeaker`** (Linux/macOS) replaces `KokoroSpeaker` in `open_speaker()` and keeps its interface (`warm_up`, `prefetch`, `begin` → a `Playback`). Each sentence is one engine request (`start` with `text` and `"end": true`), with the voice's plugin speed from `VOICE_SETTINGS` (M24). `prefetch` sends the request early; `begin` uses a matching prefetched request or cancels it and starts anew, exactly mirroring today's future-based logic. The returned PCM is written as a 24 kHz mono 16-bit WAV in `<project>/play/` and played with today's `play_file`. `warm_up` becomes `ensure` + `warm`.
- `drain()`, locks, leases, `MAX_ENGINES` (now a cap on audible projects, not models — M2), the jump rule, stop requests, cutover and sticky items are untouched.
- **Fail-silent (M23):** engine unreachable or `ensure` times out → `open_speaker()` returns None → today's 30 s back-off. An engine error mid-item behaves like today's failed sentence (skip the rest of the item).
- **One-shot speaker** (`--voice … [--stream]`, setup test, voice preview): `ensure`, one request, play frames per sentence as they arrive (streaming) or once complete. It keeps `<state>/tts.lock` exactly as today.
- **Kill switch / interrupt:** unchanged patterns, so they kill drainers and one-shot speakers (both still named `kokoro_voice.py`); their disconnects cancel their requests in the engine. The engine and the assistant are untouched (M18). `tts_controller.py stop --all` additionally runs `voiceover-engine stop`.
- **What "stop" means under socket activation** *(builder-check call)*. `voiceover-engine stop` is about silencing right now. It ends every request and exits the engine, and the next connection starts it again; that's intended, since the assistant should not stay mute. Keeping the engine down takes two new launcher commands:
  - `disable` stops and disables the socket unit, and also stops a self-started engine. It stays down until `enable` or setup.
  - `enable` first stops any self-started engine, so it can't hold the socket path or the port, then enables and starts the socket unit.
  - `ensure` always tries to connect first. When the unit is installed and enabled, systemd answers, and `ensure` never self-starts beside it.
- **No model:** the engine replies `no_model` to `start`/`warm` and never downloads (M25). Narration's `resolve_engine` → `none` still means no drainer at all. Dry-run keeps `DryRunSpeaker` and never contacts the engine.

### 5.9 Setup and platforms

- **Opt-in only** *(builder-check call)*. The plugin is public, so ordinary narration users get no systemd unit and no TCP listener: their narration uses the self-started engine (5.3). Only people who opt into "share the engine with another program" get the unit.
- **Linger.** A user socket unit only listens while the user's systemd manager runs. Setup checks `loginctl show-user $USER -p Linger`. If it's off, setup warns that the engine is reachable only while the user is logged in, and gives the command to enable it. It doesn't run that command itself. (On the owner's machine linger is already on, and `my-agent-dashboard.service` runs as a user service.)
- `/voiceover:setup` (via `scripts/setup_tts.py`) gains that opt-in "assistant engine" step on Linux: install the launcher, create the token, ask for a port (offer a free default; never 4321 or 8737; validate it is free), write `engine_port`, install and enable the systemd socket unit, and print the token with one line saying to put it in the assistant's secrets. `voiceover-engine token` prints it again; `rotate-token` replaces it.
- Changing `engine_port` rewrites the socket unit and restarts the socket.
- **macOS:** shared engine, self-started only (no launchd). The assistant runs on the Linux machine; launchd activation needs `launch_activate_socket` through ctypes and is deferred.
- **Windows:** 1.5.0 behaviour (D11).

### 5.10 Updates (M14)

The hooks refresh `current.json` and the launcher (5.3). A running engine compares its own plugin root with `current.json` (checked at most every 10 s); if they differ it exits at the next quiet moment (5.6), and the next connection starts the new version. Requests in flight always finish first.

**Pointer rules** *(builder-check calls)*:
- **Environment first.** The hooks never point at a version whose uv environment is not built. When the pointer would change, they start `voiceover-engine prepare <plugin_root>` detached; it runs `uv sync` for the environment the device setting needs, and only on success replaces `current.json`. So no caller ever waits for an environment build.
- **Highest version wins; a tie keeps the current pointer.** When the installed copy and a checkout loaded as a plugin folder both run hooks, the pointer does not flip between them. To point at a checkout of the same version, set the machine setting `engine_plugin_root`, which overrides the rule. Socket and launcher paths never change. The old version folder may vanish under a running engine; imports are already loaded (S7).

## 6. Delivery order

Step 2 does not wait for the my_agent probe; the probe's findings feed INTERFACES.md before release.

1. **Stub listener** first: the launcher's `serve`, socket activation, both listeners, token check, hello, and a `start` that streams a sine tone in the real frame format. Send the port to the my_agent session for its boxed probe (tunnel, token line, post-interrupt latency) before INTERFACES.md is final.
2. The real engine (model, scheduler, cancel, limits, lifecycle, fallback, release, status).
3. Drainer and one-shot speaker as clients; kill-switch split.
4. Setup step, systemd units, launcher refresh from the hooks.
5. INTERFACES.md (protocol 1), README, CHANGELOG; release 1.6.0.

## 7. Acceptance

T1–T16 of the requirements, with T8 read against D4 (defaults 330 s shared / 60 s narration-only; test with 10 s), plus the tests below.

Conditions for the latency tests: a short sentence (2 to 5 words), the engine on the card, and nothing else in the model unless the test says so. T20 is measured once that way. It's then measured again with the killed client's sentence still rendering; there, allow one extra sentence time (about 0.7 s total) and record the figure.

| # | Scenario | Pass when |
|---|---|---|
| T17 | TCP connection with no, wrong, then right token. | `unauthorized` and close twice; then hello. Unix socket needs no token. |
| T18 | Engine not running; a client connects, sends `warm`, then 2 s later a short `start`. | First audio within 0.5 s of `start`. |
| T19 | A connection stays open and idle past `idle_unload_s`. | Engine exits on schedule; the card list shows no plugin process. |
| T20 | Loaded engine; client killed mid-utterance; new connection sends auth, hello, `start` "Okay." | First audio within 0.5 s of the `start` line. |
| T21 | Narration first sound at prompt submit vs 1.5.0 (prewarm path), warm and cold. | No later than 1.5.0. |
| T22 | `engine_port` unset. | Nothing listens on any TCP port. |

## 8. Left to the builder

Internal module layout of the engine; asyncio vs selectors; exact buffer sizes and connection caps within 5.5; the clause-split rule for D7; the free-port default offered by setup; how the hooks rate-limit the pointer refresh; test structure (the existing harness in `tests/`); S1 memory tuning (measure each option on the card; adopt only what keeps M19 and does not slow S3).
