# Shared Kokoro Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One process holds the Kokoro model and serves both Claude Code narration (Unix socket) and the owner's voice assistant (token-checked loopback TCP), started on demand by systemd socket activation or by the plugin itself.

**Architecture:** A stdlib asyncio server (`tts/engine_server.py`) speaks protocol 1 over adopted or self-bound listeners and feeds one inference worker through a sentence-level round-robin scheduler. The model lives behind a small `Synth` interface: `KokoroSynth` in production, `SineSynth` for the stub milestone and the tests. Drainers and the one-shot speaker become stdlib clients, so every narration rule stays where it is. A stdlib launcher handles starting, systemd units and the version pointer, which the hooks refresh.

**Tech Stack:** Python 3.10+ standard library (asyncio, socket, fcntl, wave, array, secrets, hmac), kokoro-onnx / onnxruntime(-gpu) / numpy inside the engine only, systemd user units, `uv`.

**Spec:** [docs/superpowers/specs/2026-11-18-shared-kokoro-engine-design.md](../specs/2026-11-18-shared-kokoro-engine-design.md). It builds on [the requirements](../specs/2026-11-18-kokoro-shared-engine-requirements.md); M-, S- and T-numbers refer to those. Read both before starting.

## Global Constraints

- `hooks/` and `voiceover/` import nothing outside the standard library. New `tts/engine_*.py` files are stdlib-only too; only `tts/kokoro_engine.py` and `tts/kokoro_model.py` may import kokoro-onnx, onnxruntime or numpy, and only inside functions (lazily).
- Tests follow the existing harness: plain scripts `tests/test_*.py` using `_harness.isolate()`, `check()` and `report()`, run by `tests/run_all.sh`. No pytest. Kokoro, numpy and onnxruntime are stubbed.
- Protocol version `1`. Audio is `{"format":"pcm_s16le","rate":24000,"channels":1}`. Limits are `{"line":65536,"text":6000,"frame":32768}`.
- Error codes: `bad_request`, `unsupported_protocol`, `unknown_voice`, `bad_speed`, `text_too_long`, `busy`, `no_model`, `synthesis_failed`, `released`, `restarting`, `unauthorized`.
- Speed range is 0.5–2.0. Without a speed, the voice's `VOICE_SETTINGS` speed applies, else 1.0.
- Idle unload: the `engine_idle_s` setting, else 330 s when `engine_port` is set, else 60 s.
- Paths: `<state>/engine/` (mode 0700) holds `engine.sock` (0600), `engine.lock`, `start.lock`, `token` (0600, 64 hex characters), `current.json`, `keep_off.json` and `engine.log`. The launcher shim lives at `<state>/bin/voiceover-engine`.
- TCP binds `127.0.0.1` only, and only when `engine_port` is set. Every accepted TCP socket gets `TCP_NODELAY`.
- The engine script is named `kokoro_engine.py`. It must never match the kill patterns, which match `kokoro_voice.py`. Drainers and the one-shot speaker keep the name `kokoro_voice.py`.
- Windows keeps 1.5.0 behaviour: in-process `KokoroSpeaker`, spawned with `uv run`.
- Narration stays fail-silent: hooks exit 0 and print nothing, whatever the engine does.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **UTF-8 split across reads.** A multi-byte character (an emoji, or a stray Hebrew word) split across two TCP reads must decode correctly, never as an error or mojibake. The test is in Task 1.
2. **Text with no sentence punctuation, or only whitespace.** `start` "ok" then `end` must speak "ok". `start` "   " with `"end":true` must give `end`/`complete` with `samples` 0 and no synthesis call. The tests are in Tasks 3 and 5.
3. **A caller that vanishes mid-frame.** A broken pipe while writing must not crash the engine or delay other connections. The test is in Task 5.
4. **A stale socket after SIGKILL.** A self-bound engine killed with SIGKILL leaves `engine.sock` behind. The next `serve` must bind without cleanup (M21). The test is in Task 6.
5. **Request ids on one connection.** After a cancel, the next request on the same connection is still served, and its audio is never labelled with the cancelled id. Reusing an id that is still active gets `bad_request`. The test is in Task 5.

---

## File map

| File | Responsibility |
|---|---|
| `tts/engine_protocol.py` (new) | Constants, line encoding, pipelined line reader, PCM conversion, framing |
| `tts/engine_paths.py` (new) | Engine folder and file paths, token, uncached settings, pointer and keep-off files |
| `tts/engine_text.py` (new) | Incremental sentence buffer, clause split, live first-clause split |
| `tts/engine_scheduler.py` (new) | Round-robin sentence scheduler with live priority |
| `tts/engine_server.py` (new) | Listeners, connections, ops, worker thread, lifecycle |
| `tts/engine_stub.py` (new) | `SineSynth`, the model-free `Synth` |
| `tts/engine_client.py` (new) | `EngineConnection` (threaded reader), `ensure()` |
| `tts/engine_launcher.py` (new) | CLI: serve, ensure, prepare, status, release, stop, token, systemd units |
| `tts/voiceover_engine_shim.py` (new) | The file copied to `<state>/bin/voiceover-engine` |
| `tts/kokoro_model.py` (new) | Model paths, download, voice settings, device choice and loading (moved out of `kokoro_voice.py`), `KokoroSynth` |
| `tts/kokoro_engine.py` (new) | Engine entry point |
| `tts/engine_speaker.py` (new) | `EngineSpeaker`, the drainer's speaker backed by the engine |
| `tts/kokoro_voice.py` | Uses `kokoro_model`; `open_speaker` and one-shot go through the engine except on Windows |
| `tts/engine_common.py` | `drain()` calls `speaker.idle()` while it waits on a lease |
| `tts/tts_controller.py` | `stop --all` |
| `voiceover/engine_pointer.py` (new) | Pointer refresh, shim install, prepare spawn (hook side) |
| `voiceover/speech.py` | Drainer and one-shot commands run plain `python3` except on Windows |
| `voiceover/settings.py`, `scripts/manage_settings.py` | Machine settings `engine_port`, `engine_idle_s`, `engine_plugin_root` |
| `hooks/user_prompt_submit.py` | Calls the pointer refresh before the prewarm |
| `scripts/setup_tts.py`, `commands/setup.md` | Opt-in "share the engine" step |
| `INTERFACES.md`, `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json` | Protocol 1 and release 1.6.0 |

---

### Task 1: Protocol primitives

**Files:**
- Create: `tts/engine_protocol.py`
- Test: `tests/test_engine_protocol.py`

**Interfaces:**
- Produces:
  - `PROTOCOL = 1`; `AUDIO: dict`; `LIMITS: dict`; `ERROR_CODES: frozenset`
  - `encode_line(obj: dict) -> bytes`: compact JSON, UTF-8, ending in `b"\n"`
  - `class BadLine(NamedTuple): code: str; message: str`
  - `class LineReader`, with `feed(data: bytes) -> list[dict | BadLine]`. It buffers bytes, decodes only complete lines, and accepts any number of lines in one chunk. A line longer than `LIMITS["line"]` yields `BadLine("bad_request", …)`, and the reader then discards bytes up to the next newline. Lines that aren't valid JSON, or aren't an object, yield `BadLine("bad_request", …)`.
  - `pcm16le(samples) -> bytes`: floats in [-1, 1] become clipped little-endian int16. It uses `astype` when `samples` has one (numpy), else `array("h")`, byte-swapped on big-endian hosts.
  - `split_frames(pcm: bytes, size: int = LIMITS["frame"]) -> list[bytes]`: every piece is even-length and at most `size`.
  - `audio_header(req_id: str, seq: int, nbytes: int) -> bytes`: the `{"ev":"audio","id","seq","bytes"}` line.

- [ ] **Step 1: Write the failing tests**

```python
r = LineReader()
check("pipelined lines in one chunk", r.feed(b'{"op":"auth","token":"t"}\n{"op":"hello","proto":1}\n{"op":"st') ==
      [{"op": "auth", "token": "t"}, {"op": "hello", "proto": 1}])
check("rest of a line completes later", r.feed(b'atus"}\n') == [{"op": "status"}])
emoji = '{"op":"text","id":"u","text":"hi \U0001F600"}\n'.encode()
r2 = LineReader(); first = r2.feed(emoji[:-4]); second = r2.feed(emoji[-4:])
check("utf-8 split across reads", first == [] and second[0]["text"] == "hi \U0001F600")
r3 = LineReader(); out = r3.feed(b"x" * 70000 + b"\n" + b'{"op":"status"}\n')
check("over-long line -> bad_request, then recovers",
      isinstance(out[0], BadLine) and out[0].code == "bad_request" and out[1] == {"op": "status"})
check("non-object JSON -> bad_request", isinstance(LineReader().feed(b"[1]\n")[0], BadLine))
check("pcm clip and little-endian", pcm16le([0.0, 1.0, -1.0, 2.0]) == b"\x00\x00\xff\x7f\x01\x80\xff\x7f")
frames = split_frames(b"\x01\x00" * 40000)
check("frames even and capped", all(len(f) % 2 == 0 and len(f) <= 32768 for f in frames) and sum(map(len, frames)) == 80000)
check("audio header", json.loads(audio_header("u1", 0, 32768)) == {"ev": "audio", "id": "u1", "seq": 0, "bytes": 32768})
check("unauthorized is an error code", "unauthorized" in ERROR_CODES)
```

- [ ] **Step 2: Run them and check they fail.** Run `python3 tests/test_engine_protocol.py`. Expected: `ModuleNotFoundError: engine_protocol`.
- [ ] **Step 3: Implement `tts/engine_protocol.py`** to the Interfaces above.
- [ ] **Step 4: Run them and check they pass.** Run `python3 tests/test_engine_protocol.py`. Expected: all checks PASS.
- [ ] **Step 5: Commit.** `git add tts/engine_protocol.py tests/test_engine_protocol.py && git commit -m "feat(engine): protocol 1 primitives"`

---

### Task 2: Engine paths, token, settings, pointer, keep-off

**Files:**
- Create: `tts/engine_paths.py`
- Modify: `voiceover/settings.py:20,142` (adds `engine_port: None`, `engine_idle_s: None`, `engine_plugin_root: None` to `DEFAULTS` and to `MACHINE_SETTINGS`), `scripts/manage_settings.py:118` (validation: port is an int from 1 to 65535 or null, idle is a number of at least 5 or null, plugin root is an existing directory or null)
- Test: `tests/test_engine_paths.py`, and extend `tests/test_tts_device.py` only if it lists `MACHINE_SETTINGS`

**Interfaces:**
- Consumes: `engine_common.data_dir()`
- Produces:
  - `engine_dir() -> Path` (created with mode 0700, and chmod-ed to 0700 if it already exists)
  - `socket_path()`, `lock_path()`, `start_lock_path()`, `token_path()`, `pointer_path()`, `keep_off_path()`, `log_path()`, each `-> Path`
  - `read_or_create_token() -> str`: creates the file atomically with `O_CREAT|O_EXCL` and mode 0600, and returns the existing value if the file is already there
  - `rotate_token() -> str`
  - `read_global_settings() -> dict`: uncached; `{}` on any error
  - `engine_port() -> int | None` (out-of-range values count as None)
  - `idle_unload_seconds() -> float`
  - `read_pointer() -> dict | None`; `write_pointer(info: dict) -> None` (temp file + `os.replace`)
  - `read_keep_off() -> dict | None` (returns None once `until` has passed); `write_keep_off(seconds: float, reason: str) -> None`

- [ ] **Step 1: Write the failing tests.**
  - Folder mode is 0700. The token is 64 hex characters with mode 0600, the same on a second call, and different after `rotate_token()`.
  - `idle_unload_seconds()` gives 60 with no settings, 330 with `{"engine_port": 47320}`, and 12 with `{"engine_port": 47320, "engine_idle_s": 12}`.
  - `engine_port()` is None for `70000`.
  - `read_keep_off()` returns the record for `write_keep_off(30, "release")`, and None after one written with -1.
  - A pointer round-trips through `write_pointer` and `read_pointer`.
  - `settings.MACHINE_SETTINGS` contains the three new keys.
- [ ] **Step 2: Run them and check they fail.** Run `python3 tests/test_engine_paths.py`. Expected: FAIL (no module).
- [ ] **Step 3: Implement** `tts/engine_paths.py` and the settings changes.
- [ ] **Step 4: Run them and check they pass.** Run `python3 tests/test_engine_paths.py && bash tests/run_all.sh`. Expected: all pass.
- [ ] **Step 5: Commit.** Message: `feat(engine): state paths, token and machine settings`.

---

### Task 3: Incremental sentence text

**Files:**
- Create: `tts/engine_text.py`
- Test: `tests/test_engine_text.py`

**Interfaces:**
- Consumes: `engine_common.split_sentences` (the sentence rule, M8)
- Produces:
  - `CLAUSE_SPLIT_WORDS = 40`; `LIVE_FIRST_CLAUSE_MIN_WORDS = 4`
  - `split_long(sentence: str, max_words: int = CLAUSE_SPLIT_WORDS) -> list[str]`: splits at the clause boundary (`,` `;` `:` ` - ` or an em dash) nearest the middle, recursing until every piece has at most `max_words` words. With no boundary, it cuts at `max_words` words.
  - `class UtteranceText(live: bool = False)`:
    - `feed(text: str) -> list[str]`: returns the sentences that are now complete. A sentence counts as complete once a sentence break (`.`, `!` or `?` followed by whitespace, or a newline) has been seen after it. The trailing partial sentence is held back.
    - `drain() -> list[str]`: everything still buffered, for flush or end.
    - `chars: int`: total characters received.
    - With `live=True`, only the very first sentence returned is further split at its first clause boundary after at least 4 words.

- [ ] **Step 1: Write the failing tests**

```python
u = UtteranceText()
check("partial held back", u.feed("The build passed. I am now run") == ["The build passed."])
check("fragment joins", u.feed("ning the tests") == [] and u.drain() == ["I am now running the tests"])
check("no punctuation spoken on drain", UtteranceText().feed("ok") == [] )
w = UtteranceText(); w.feed("ok"); check("drain gives ok", w.drain() == ["ok"])
ws = UtteranceText(); check("whitespace only -> nothing", ws.feed("   ") == [] and ws.drain() == [])
long = " ".join(["word"] * 30) + ", " + " ".join(["more"] * 30) + "."
check("long sentence split at clause", all(len(p.split()) <= 40 for p in split_long(long)) and len(split_long(long)) == 2)
check("no boundary -> 40-word cuts", [len(p.split()) for p in split_long(" ".join(["w"] * 90))] == [40, 40, 10])
lv = UtteranceText(live=True)
check("live first clause early (first boundary after >= 4 words)",
      lv.feed("Well, sure thing, I can check the build now. Next. ") ==
      ["Well, sure thing,", "I can check the build now.", "Next."])
nl = UtteranceText()
check("non-live first sentence not split", nl.feed("Well, sure thing, I can check the build now. ") ==
      ["Well, sure thing, I can check the build now."])
```
- [ ] **Step 2: Run them and check they fail.** Run `python3 tests/test_engine_text.py`. Expected: FAIL.
- [ ] **Step 3: Implement** `tts/engine_text.py`.
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS.
- [ ] **Step 5: Commit.** Message: `feat(engine): incremental sentence buffer and clause split`.

---

### Task 4: Sentence scheduler

**Files:**
- Create: `tts/engine_scheduler.py`
- Test: `tests/test_engine_scheduler.py`

**Interfaces:**
- Produces:
  - `class Job(NamedTuple): conn: int; req: str; text: str; live: bool`
  - `class Scheduler`, with these methods:
    - `add(job: Job) -> None`
    - `next() -> Job | None`
    - `cancel(conn: int, req: str | None = None) -> int`: drops queued jobs and returns how many were dropped
    - `has_work() -> bool`
    - `pending(conn: int) -> int`
  - Rule, for M17 plus D6. Jobs within a connection are strictly first-in, first-out. Scheduling works in rounds. Each round serves every connection that had a ready job when the round began, once. Within a round, connections whose head job is `live` go first; otherwise the order is the order in which they started waiting. A connection already served this round waits for the next round.

- [ ] **Step 1: Write the failing tests.**
  - Connection A has 3 jobs and B has 2. `next()` gives A, B, A, B, A.
  - Add C with a live job while A and B wait. The next round starts with C.
  - A live connection with 5 queued jobs, plus narration N with 2: the order is L, N, L, N, L, L, L. This proves narration never waits more than one sentence.
  - `cancel(conn, "r1")` removes only r1's jobs. `cancel(conn)` removes all of that connection's jobs.
  - `has_work()` is False when the scheduler is empty.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement** `tts/engine_scheduler.py` (stdlib; `collections.deque` per connection).
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS.
- [ ] **Step 5: Commit.** Message: `feat(engine): round-robin sentence scheduler with live priority`.

---

### Task 5: Engine server over the Unix socket, with SineSynth

**Files:**
- Create: `tts/engine_server.py`, `tts/engine_stub.py`
- Test: `tests/test_engine_server.py` (starts the server in a background thread on a temp socket and drives it with raw sockets and `engine_protocol`)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - The `Synth` interface (duck-typed and documented in a docstring):
    - `loaded: bool`
    - `load() -> None`: may raise `NoModel`
    - `voices() -> set[str]`
    - `default_speed(voice: str) -> float`
    - `synthesize(text: str, voice: str, speed: float, lang: str) -> bytes`: returns pcm_s16le
    - `status() -> dict`, with keys `device`, `requested`, `fallback` and `vram_mib`
  - `class NoModel(Exception)` in `engine_server`
  - `class SineSynth` in `engine_stub.py`:
    - `__init__(load_delay=0.0, synth_delay=0.0, voices=("af_sky","bf_emma"))`
    - Produces a 440 Hz tone of 0.06 s per word at 24 kHz.
  - `class EngineServer`:
    - `__init__(synth, *, idle_unload_s: float, token: str | None, plugin_version: str, plugin_root: str, max_connections: int = 16, max_requests: int = 64, out_buffer_bytes: int = 2 * 1024 * 1024, slow_reader_s: float = 30.0)`
    - `serve_forever(listeners: list[socket.socket]) -> str`: blocking; runs its own asyncio loop; returns the exit reason, one of `"idle"`, `"stop"`, `"release"`, `"update"` or `"card_return"`.
    - `request_exit(reason: str) -> None`: thread-safe
  - Behaviour, all from design 5.4–5.5 and the requirements' section 5:
    - `hello` reply: `proto`, `plugin`, `audio`, `limits`, `loaded`, `device`. It never loads the model. An unsupported `proto` gets the `unsupported_protocol` error and close.
    - `start`, `text`, `flush`, `end` and `cancel` behave as specified, and so do `cancel` without an id, `status` and `stop`. `warm` is in Task 7.
    - Exactly one `end` per started request, with `complete`, `cancelled` or `error`. An `error` line comes before `end`/`error`.
    - Unknown op: `bad_request`. Unknown fields are ignored.
    - `lang` defaults to `"en-us"`. Speed defaults to `synth.default_speed(voice)`. Voice validation runs after the model loads (the voice file is the source): `unknown_voice`. Speed outside 0.5–2.0: `bad_speed`. More than 6000 characters: `text_too_long`, and the request ends with `error`.
    - Too many connections: `busy` and close. Too many queued requests: `busy` for that request.
    - The model loads on the first `start` (or `warm`) in the worker thread. `NoModel` gives `no_model` for that request.
    - One worker thread pulls `Scheduler.next()`. Audio goes out with `audio_header`, then the frame, the first frame as soon as its sentence is rendered. `samples` in `end` is the total number of samples sent.
    - A cancel, or a disconnect, drops queued jobs and discards a sentence in flight. The `end`/`cancelled` line is written at once.
    - Output buffer full for `slow_reader_s`: the connection is dropped. Writes never block the worker.

- [ ] **Step 1: Write the failing tests** (hello, T2-shape, T4-shape, cancel timing, errors, Review Focus 2, 3 and 5)

```python
srv, path = start_server(SineSynth(synth_delay=0.05))
c = Raw(path)                       # helper: send(obj), read_event(), read_audio()
c.send({"op": "hello", "proto": 1}); h = c.read_event()
check("hello", h["ev"] == "hello" and h["audio"] == {"format": "pcm_s16le", "rate": 24000, "channels": 1}
      and h["loaded"] is False)
c.send({"op": "start", "id": "u2", "voice": "bf_emma", "text": "Hello there.", "end": True})
evs = c.read_until_end("u2")
pcm = b"".join(e.payload for e in evs if e.ev == "audio")
check("bytes == 2*samples", len(pcm) == 2 * evs[-1]["samples"] and evs[-1]["reason"] == "complete")
# fragments + flush (T4)
c.send({"op": "start", "id": "u1", "voice": "bf_emma"}); c.send({"op": "text", "id": "u1", "text": "One. Tw"})
check("first sentence audio before rest", c.read_event()["ev"] == "audio")
c.send({"op": "text", "id": "u1", "text": "o"}); c.send({"op": "flush", "id": "u1", "n": 1})
check("flushed after its audio", "flushed" in [e["ev"] for e in c.read_until("flushed")])
# cancel within 0.3 s, then the next request is served under its own id (Review Focus 5)
c.send({"op": "start", "id": "long", "voice": "bf_emma", "text": "Word. " * 60, "end": True}); c.read_event()
t = time.time(); c.send({"op": "cancel", "id": "long"}); end = c.read_until_end("long")[-1]
check("cancelled within 0.3 s", end["reason"] == "cancelled" and time.time() - t < 0.3)
c.send({"op": "start", "id": "next", "voice": "bf_emma", "text": "Hi.", "end": True})
check("next served, no stray ids", {e["id"] for e in c.read_until_end("next")} == {"next"})
c.send({"op": "start", "id": "dup", "voice": "bf_emma"}); c.send({"op": "start", "id": "dup", "voice": "bf_emma"})
check("active id reuse -> bad_request", c.read_error()["code"] == "bad_request")
check("unknown voice", err_for(c, {"voice": "zz_top"}) == "unknown_voice")
check("bad speed", err_for(c, {"speed": 3.0}) == "bad_speed")
check("6001 chars", err_for(c, {"text": "a" * 6001, "end": True}) == "text_too_long")
check("whitespace-only completes with 0 samples", end_for(c, {"text": "   ", "end": True}) == ("complete", 0))
# vanishing caller mid-frame (Review Focus 3)
d = Raw(path); d.hello(); d.send({"op": "start", "id": "x", "voice": "bf_emma", "text": "Word. " * 40, "end": True}); d.close_abruptly()
c.send({"op": "start", "id": "after", "voice": "bf_emma", "text": "Still here.", "end": True})
check("others unaffected by a broken pipe", c.read_until_end("after")[-1]["reason"] == "complete")
check("status does not load", fresh_status(path)["loaded"] is False)
```
   Also add a slow-reader test: build with `slow_reader_s=1.0` and `out_buffer_bytes=65536`, and have a client that never reads. It must be dropped within 2 s while another client completes (T15).
- [ ] **Step 2: Run them and check they fail.** Run `python3 tests/test_engine_server.py`. Expected: FAIL (no module).
- [ ] **Step 3: Implement** `tts/engine_stub.py` and `tts/engine_server.py`. Use asyncio streams on the given listening sockets (`asyncio.start_unix_server(sock=…)`, or `start_server(sock=…)` for TCP). The worker is a `threading.Thread`. Results go back to the loop with `loop.call_soon_threadsafe`.
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS, with the suite under about 20 s.
- [ ] **Step 5: Commit.** Message: `feat(engine): protocol-1 server with sentence scheduling (stub synth)`.

---

### Task 6: Listeners: systemd adoption, self-bind, TCP with token

**Files:**
- Modify: `tts/engine_server.py`
- Test: `tests/test_engine_listeners.py`

**Interfaces:**
- Consumes: Tasks 2 and 5.
- Produces, in `engine_server.py`:
  - `adopt_systemd_listeners(environ=os.environ) -> list[socket.socket] | None`: uses `LISTEN_FDS`/`LISTEN_PID` (the pid must equal `os.getpid()`). Fds start at 3. Each socket's family is read from `getsockname`. The variables are unset after reading.
  - `bind_listeners(sock_path: Path, port: int | None) -> list[socket.socket]`: the caller already holds `engine.lock`. A leftover socket file is unlinked, then bound with mode 0600 inside the 0700 folder. With a port, `127.0.0.1:port` is bound with `SO_REUSEADDR`.
  - `hold_single_instance(lock: Path) -> int | None`: a non-blocking `flock`. It returns the fd, or None when another engine holds the lock.
  - In `EngineServer`: on a TCP connection the first line must be `{"op":"auth","token":…}`, compared with `hmac.compare_digest`. Otherwise the server sends `unauthorized` and closes. Lines pipelined after a valid auth are processed in the same pass. On the Unix socket an `auth` line is accepted and ignored. `TCP_NODELAY` is set on every accepted TCP socket.

- [ ] **Step 1: Write the failing tests.**
  - **T17, token handling.** Over TCP (a free port from `bind(("127.0.0.1", 0))`), these three connections each get `unauthorized` and then EOF: no token, a wrong token, and `hello` sent first. A single write carrying a right token, `hello` and `start` gets `hello` and then audio.
  - **No token on the Unix socket.** The Unix socket answers `hello` without any token.
  - **NODELAY.** `TCP_NODELAY` is set on the server-side socket. Expose `EngineServer.last_tcp_nodelay` for the test.
  - **Stale socket (Review Focus 4).** Create a regular file at the socket path. `bind_listeners` must still succeed.
  - **Single instance.** A second `hold_single_instance` returns None while the first fd is open, and returns an fd after the first is closed.
  - **systemd adoption.** In a forked child, set `LISTEN_FDS=1` and `LISTEN_PID=<child pid>` with a listening socket dup2'd to fd 3. `adopt_systemd_listeners()` must return one socket.
  - **T22.** With no port, `bind_listeners` returns only the Unix socket.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS, with `bash tests/run_all.sh` still green.
- [ ] **Step 5: Commit.** Message: `feat(engine): systemd and self-bound listeners, token-checked TCP`.

---

### Task 7: Lifecycle: warm, idle exit, quiet-moment exits, release, stop

**Files:**
- Modify: `tts/engine_server.py`
- Test: `tests/test_engine_lifecycle_server.py`

**Interfaces:**
- Consumes: Tasks 2, 5 and 6.
- Produces, in `EngineServer`:
  - `warm {hold_s}` (default 0, capped at 600): starts loading if needed, then replies `{"ev":"warm","loaded":true,"device":…}` once loaded. The engine stays loaded until `max(now + hold_s, last_activity + idle_unload_s)`.
  - `release {keep_off_s}`: `write_keep_off(keep_off_s, "release")` when the value is above 0. Replies `{"ev":"released","vram_mib":0}`. Every open request then gets `error` with code `restarting`, followed by `end`/`error`. `serve_forever` returns `"release"`.
  - `stop`: every open request gets `end`/`cancelled`, and `serve_forever` returns `"stop"`.
  - **Idle.** No queued or active request, no warm hold, and `idle_unload_s` passed since the last activity. Open idle connections don't count (D4). It returns `"idle"`.
  - **Quiet-moment checks**, every 10 s; the interval is a constructor argument `quiet_check_s=10.0`, made small in tests. Each check runs only when nothing is queued or active, no warm hold remains, and no connection is open:
    - `read_pointer()["plugin_root"]` differs from the server's `plugin_root`, or the `engine_plugin_root` setting differs: return `"update"`.
    - The device setting changed (`read_global_settings().get("tts_device")` differs from the value at start): return `"update"`.
    - `synth.status()["fallback"]["reason"] == "keep_off"` and `read_keep_off()` is None (the keep-off has expired): return `"card_return"`. A fallback for any other reason (load or synthesis failure) stays on CPU until the idle exit.
  - `status` adds `warm_until`, `idle_unload_s`, `idle_s`, `uptime_s`, `callers`, `requests`, `leases` (the number of active warm holds), `pid`, `plugin` and `proto`.

- [ ] **Step 1: Write the failing tests.**
  - **Idle exit (T8 shape).** With `idle_unload_s=0.5`, the server exits with "idle" within 1.5 s of the last request.
  - **Idle connection (T19).** An open, idle connection doesn't delay that exit.
  - **Warm hold.** `warm` with `hold_s=2` keeps the server alive for 2 s even with `idle_unload_s=0.5`, and the `warm` reply arrives.
  - **Release.** `release` with `keep_off_s=30` writes keep-off, an in-flight request gets `restarting`, and the result is "release".
  - **Stop.** `stop` returns "stop".
  - **Update.** A pointer pointing elsewhere makes the server return "update" only after the last connection closes. While a client is connected and idle, it does not exit within 3 × `quiet_check_s`.
  - **Status.** It reports `idle_unload_s`.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS.
- [ ] **Step 5: Commit.** Message: `feat(engine): warm holds, idle and quiet-moment exits, release and stop`.

---

### Task 8: Client, launcher, shim, stub serve, and the probe milestone

**Files:**
- Create: `tts/engine_client.py`, `tts/engine_launcher.py`, `tts/voiceover_engine_shim.py`, `tts/kokoro_engine.py` (with only the `--stub` path working in this task)
- Test: `tests/test_engine_launcher.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 5, 6 and 7.
- Produces:
  - `engine_client.EngineError(code: str, message: str)`
  - `engine_client.EngineConnection`:
    - `connect(path: Path | None = None, timeout: float = 2.0) -> EngineConnection`: a classmethod. It sends `hello` and checks `proto == 1`, raising `EngineError("unsupported_protocol")` otherwise. A background reader thread routes events by id.
    - `submit(text: str, voice: str, speed: float | None = None, live: bool = False) -> Pending`: sends `start` with `"end": true`.
    - `cancel(p: Pending) -> None`
    - `warm(hold_s: float = 0) -> None`: fire and forget
    - `status() -> dict`
    - `release(keep_off_s: float = 0) -> dict`
    - `stop() -> None`
    - `close() -> None`
    - `alive: bool`
  - `engine_client.Pending`:
    - `id: str`
    - `done: threading.Event`
    - `chunks: queue.Queue[bytes | None]`: `None` marks the end
    - `pcm() -> bytes`: blocks until done
    - `reason: str | None`
    - `error: EngineError | None`
  - The environment variable `VOICEOVER_ENGINE_STUB=1` (tests and the probe only) makes `ensure` add `--stub` to the spawned `serve` command.
  - `engine_client.ensure(plugin_root: Path, timeout: float = 10.0) -> bool`:
    1. Try to connect; return True if that works.
    2. Otherwise take an exclusive `flock` on `start.lock`, then retry the connect.
    3. If that also fails, spawn `[sys.executable, <plugin_root>/tts/engine_launcher.py, "serve", "--plugin-root", <plugin_root>]` detached (`start_new_session=True`, stdio sent to `engine.log`).
    4. Poll the connect every 50 ms until it succeeds, or until `timeout` passes.
  - `engine_launcher.main(argv: list[str]) -> int`, with these subcommands:
    - `serve [--plugin-root P] [--stub]`. With `--stub`, it execs `sys.executable <root>/tts/kokoro_engine.py --stub`. Otherwise it execs `pointer["python"] <root>/tts/kokoro_engine.py`. Exec keeps the pid, so `LISTEN_PID` stays valid. If the pointer has no `python`, it runs `prepare` first.
    - `ensure`, `status` (prints the JSON), `release [--keep-off S]`, `stop`, `token`, `rotate-token`
    - `prepare` and the systemd subcommands come in Tasks 9 and 10.
    - Exit codes: 0 means ok, 1 means engine unreachable, 2 means usage.
  - `voiceover_engine_shim.py`: stdlib. It reads `<state>/engine/current.json`, or with `--plugin-root` uses that, puts `<plugin_root>/tts` on `sys.path`, and calls `engine_launcher.main(sys.argv[1:])`. Without a pointer it prints one line to stderr and exits 1.
  - `kokoro_engine.main(argv) -> int`:
    1. Choose listeners: systemd adoption, else `hold_single_instance` + `bind_listeners`. If another engine holds the lock, exit 0.
    2. Choose the synth: `SineSynth()` with `--stub`, else `KokoroSynth` (Task 11).
    3. Run `EngineServer(...).serve_forever`.
    4. Log the exit reason to `engine.log` and exit 0.

- [ ] **Step 1: Write the failing tests.**
  - **T10 shape.** Ten threads call `ensure(REPO_ROOT)` against a stub; set `VOICEOVER_ENGINE_STUB=1`, which makes `ensure` pass `--stub`. All return True, and `pgrep -f "kokoro_engine.py --stub"` shows exactly one process.
  - **Streaming client.** `EngineConnection.submit("Hello there.", "bf_emma")` yields chunks, then `None`, then `reason == "complete"`.
  - **CLI.** `engine_launcher.main(["status"])` exits 0 and prints `"loaded"`. `main(["token"])` prints the token file's value.
  - **SIGKILL recovery (T9 shape).** Kill the stub engine with SIGKILL. A pending client sees `alive == False` within 1 s, and the next `ensure` returns True within 3 s.
  - **Shim.** The shim run with `--plugin-root REPO_ROOT status` exits 0.
  - Clean up by calling `main(["stop"])` at the end.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement** the four files.
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS.
- [ ] **Step 5: Commit.** Message: `feat(engine): client, launcher, shim and stub engine`.
- [ ] **Step 6: Probe milestone.**
  1. Put `{"engine_port": 47320}` in your real `~/.claude-voiceover/settings.json`, or pick the first free port from 47320 upward.
  2. Run `python3 tts/engine_launcher.py serve --plugin-root "$PWD" --stub &`.
  3. Confirm `ss -ltn | grep 47320` shows `127.0.0.1:47320` only.
  4. Send the my_agent session the port and this line: "the token is printed by `python3 tts/engine_launcher.py token`".
  5. Wait for its boxed rerun. Record its numbers in the PR description draft.
  - Don't block the next tasks on this.

---

### Task 9: Version pointer, prepare, shim install (hook side)

**Files:**
- Modify: `tts/engine_launcher.py` (adds `prepare <root> --project <dir>`)
- Create: `voiceover/engine_pointer.py`
- Modify: `hooks/user_prompt_submit.py:49` (calls `engine_pointer.refresh()` just before `narrator.prewarm`, inside its own try)
- Test: `tests/test_engine_pointer.py`

**Interfaces:**
- Consumes: `speech.kokoro_project()`, `speech._PLUGIN_ROOT`, `settings.get_machine_setting`.
- Produces:
  - `engine_launcher prepare <root> --project <dir>`:
    1. Take `start.lock`.
    2. Resolve `uv` with `shutil.which`, else `~/.local/bin/uv` or `~/.cargo/bin/uv`.
    3. Run `uv sync --project <dir>`, then `uv run --project <dir> python -c "import sys; print(sys.executable)"`.
    4. Re-check the rule below against the current pointer.
    5. Write `{"plugin_root","plugin_version","uv","python","project"}`. `plugin_version` is read from `<root>/.claude-plugin/plugin.json`.
    - Exits 0 when the pointer was written or kept, and 1 on failure, leaving the pointer untouched.
  - `engine_pointer.should_replace(current: dict | None, candidate_root: Path, candidate_version: str, override: str | None) -> bool`:
    - An override wins: replace exactly when it differs from the current root.
    - Otherwise, replace when there is no current pointer, when the current root no longer exists, or when the candidate version is higher (compared as numeric tuples).
    - An equal version keeps the current pointer.
  - `engine_pointer.refresh(plugin_root: Path = speech._PLUGIN_ROOT) -> bool`. It never raises and is a no-op on Windows. It is rate-limited to once per 60 s through `<state>/engine/pointer_checked`. It does three things:
    - Installs or updates `<state>/bin/voiceover-engine` (mode 0755) when its bytes differ from `tts/voiceover_engine_shim.py`. The first line is `#!/usr/bin/env python3`.
    - Spawns `prepare` detached when `should_replace` is True, unless `VOICEOVER_NO_SPAWN` is set.
    - Returns True when it spawned.

- [ ] **Step 1: Write the failing tests.**
  - `should_replace` gives True for None, True for 1.6.1 over 1.6.0, False for 1.6.0 over 1.6.0 from another root (Review: dev checkout and installed copy), and True with an override that differs.
  - `refresh()` installs the shim with mode 0755 and identical bytes. A second call within 60 s does nothing.
  - With `VOICEOVER_NO_SPAWN`, refresh spawns nothing but still installs the shim.
  - The hook runs with no stdout, and exits 0 even with `engine_pointer.refresh` patched to raise. Use the existing `test_narrator_hooks.py` pattern.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and check they pass.** Run `python3 tests/test_engine_pointer.py && bash tests/run_all.sh`. Expected: all pass.
- [ ] **Step 5: Commit.** Message: `feat(engine): version pointer, prepare and launcher shim install`.

---

### Task 10: systemd units and the setup step

**Files:**
- Modify: `tts/engine_launcher.py` (adds `install-service`, `uninstall-service`, `enable` and `disable`)
- Modify: `scripts/setup_tts.py` (adds `--share-engine [PORT]` and `--unshare-engine`), `commands/setup.md` (a new optional step)
- Test: `tests/test_engine_systemd.py`

**Interfaces:**
- Produces:
  - `engine_launcher.socket_unit(sock: Path, port: int | None) -> str` and `engine_launcher.service_unit(python: str, shim: Path, data_dir_env: str | None) -> str`. Both return the exact text of the unit files (design 5.3).
    - The socket unit sets `ListenStream=<sock>`, `SocketMode=0600`, `DirectoryMode=0700`, an optional `ListenStream=127.0.0.1:<port>`, `NoDelay=true`, `Accept=no` and `WantedBy=sockets.target`.
    - The service unit sets `ExecStart=<python> <shim> serve`, plus `Environment=VOICEOVER_DATA_DIR=…` only when that variable is set. It has no `Restart=`.
  - `install-service` writes both units to `~/.config/systemd/user/voiceover-engine.{socket,service}`, then runs `systemctl --user daemon-reload` and `enable --now voiceover-engine.socket`.
  - `uninstall-service` runs `disable --now` and removes the files.
  - `enable` first stops a self-started engine (through `stop`, if a connect works and `systemctl --user is-active voiceover-engine.socket` is not active), then runs `enable --now`.
  - `disable` runs `disable --now` and then `stop`.
  - Every `systemctl` call goes through `engine_launcher.run_systemctl(*args) -> int`, so tests can replace it.
  - `setup_tts.py --share-engine [PORT]`:
    1. Linux only.
    2. The default port is the first free one from 47320 upward. A port that's already taken is refused, with a message naming it.
    3. Sets the `engine_port` machine setting.
    4. Runs `prepare` synchronously, then `install-service`.
    5. Checks `loginctl show-user $USER -p Linger`. If linger is off, it prints the warning plus `loginctl enable-linger $USER`, but doesn't run that command.
    6. Prints the token and the line: "Put this token in your assistant's secrets; `voiceover-engine token` shows it again."
  - `--unshare-engine` runs `uninstall-service`, clears `engine_port` and stops the engine.

- [ ] **Step 1: Write the failing tests.**
  - The unit text contains the exact lines above. Without a port it has no TCP `ListenStream`.
  - `install-service` (with `run_systemctl` faked) writes both files and calls `daemon-reload`, then `enable --now voiceover-engine.socket`, in that order.
  - `setup_tts.py --share-engine 47320` refuses when the test is already holding that port, and the refusal names the port.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement**, and add step 6 to `commands/setup.md`: "Optional: share the engine with another program (Linux): ask the user; if yes run `setup_tts.py --share-engine`."
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS.
- [ ] **Step 5: Commit.** Message: `feat(engine): systemd socket activation and opt-in setup step`.

---

### Task 11: KokoroSynth: model, voices, speed, device fallback

**Files:**
- Create: `tts/kokoro_model.py`. Move these out of `tts/kokoro_voice.py`, unchanged: `MODELS_DIR`, `MODEL_FILE`, `VOICES_FILE`, the URLs, `DEFAULT_VOICE`, `DEFAULT_SAMPLE_RATE`, `DEFAULT_VOICE_SETTINGS`, `VOICE_SETTINGS`, `get_voice_settings`, `_gpu_provider`, `choose_onnx_provider`, `_active_provider`, `_load_on`, `_load_accelerated`, `load_kokoro`, `models_present`, `download_models` and `ensure_models`.
- Modify: `tts/kokoro_voice.py` (re-import those names, so existing tests and monkeypatches keep working), `tts/kokoro_engine.py` (the real synth path)
- Test: `tests/test_kokoro_synth.py`; `tests/test_kokoro_drain.py` and `tests/test_tts_device.py` must stay green

**Interfaces:**
- Produces `kokoro_model.KokoroSynth(force_cpu: bool = False)`, which implements the `Synth` interface from Task 5:
  - **`load()`.** It raises `engine_server.NoModel` when `models_present()` is False, and never downloads. It runs `load_kokoro()`, or `_load_on("CPUExecutionProvider")` when `force_cpu` is set or `read_keep_off()` is not None. It records `requested`, `device` and `fallback` (`{"reason": "keep_off" | "load_failed: …" | "synthesis_failed: …", "at": ts}`), and imports numpy and onnxruntime eagerly (S7).
  - **`voices()`.** `set(kokoro.get_voices())`.
  - **`default_speed(v)`.** `get_voice_settings(v)["speed"]`.
  - **`synthesize(...)`.** It calls `kokoro.create(text, voice, speed, lang, trim=True)` and passes the result to `pcm16le`. If that raises on CUDA, it reloads on CPU in the same process, records the fallback and retries once. A second failure raises, and the server answers `synthesis_failed`.
  - **`status()`.** `device` is `"cuda"` or `"cpu"`. `vram_mib` comes from `nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits` filtered to `os.getpid()`, cached for 5 s, and None when unavailable.
- In `kokoro_engine.py`, the default synth is `KokoroSynth()`. `plugin_version` is read from `.claude-plugin/plugin.json` next to `tts/`. `plugin_root` is `Path(__file__).resolve().parents[1]`.

- [ ] **Step 1: Write the failing tests**, with stub `kokoro_onnx`, `numpy` and `onnxruntime` modules as in `test_kokoro_drain.py`.
  - **No model.** `load()` raises `NoModel` when `models_present` returns False.
  - **Voices.** `voices()` returns the stub's 54 names.
  - **Default speed.** It's 1.3 for `bm_daniel` and 1.0 for `af_heart`.
  - **Mid-run CUDA failure (T12 shape).** The CUDA stub raises on its second `create`. `synthesize` then returns audio, and `status()["device"] == "cpu"`, with `fallback.reason` starting with "synthesis_failed".
  - **Keep-off.** With keep-off present, `load()` uses CPU and gives `fallback.reason == "keep_off"`.
  - **Old names.** `kokoro_voice.VOICE_SETTINGS is kokoro_model.VOICE_SETTINGS`.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement** the move, `KokoroSynth` and the wiring.
- [ ] **Step 4: Run them and check they pass.** Run `bash tests/run_all.sh`. Expected: ALL TESTS PASSED.
- [ ] **Step 5: Commit.** Message: `feat(engine): KokoroSynth with CPU fallback; model helpers move to kokoro_model`.

---

### Task 12: Narration as a client

**Files:**
- Create: `tts/engine_speaker.py`
- Modify: `tts/kokoro_voice.py` (`main()`'s `open_speaker`, `speak_text`), `tts/engine_common.py:721-724` (inside the lease-wait branch, `getattr(speaker, "idle", lambda: None)()`), `voiceover/speech.py:230-246` (`kokoro_command`: on non-Windows `[sys.executable, str(_TTS_DIR / "kokoro_voice.py"), *args]`; Windows unchanged)
- Test: `tests/test_engine_speaker.py`; update `tests/test_tts_device.py:102-115` and `tests/test_stop_scope.py:52` for the new command shape

**Interfaces:**
- Consumes: `EngineConnection`, `Pending`, `ensure` (Task 8); `play_file` and `Playback` (existing).
- Produces `engine_speaker.EngineSpeaker(connect: Callable[[], EngineConnection], play_dir: Path, play_file: Callable)`:
  - `warm_up()` calls `connect()` and `warm(60)`.
  - `prefetch(sentence, voice)` submits one sentence with no speed (the engine applies the per-voice speed), keyed `(sentence, voice)`.
  - `begin(sentence, voice, upcoming=None) -> Playback` uses a matching prefetched request or submits a new one, cancels every other prefetched request, and returns an `EnginePlayback`.
  - `EnginePlayback.poll()` stays False until the request is done. It then writes `<play_dir>/<pid>-<n>.wav` with the stdlib `wave` module (1 channel, 2-byte samples, 24000 Hz) and starts `play_file`. It prefetches `upcoming` once rendering is done. A request that ended with `error` raises `EngineError` from `poll()`, which skips the rest of the item as today.
  - On `restarting` or a dead connection it reconnects through `connect()` once and resubmits.
  - `idle()` sends `warm(60)` at most every 30 s.
- `kokoro_voice.PLUGIN_ROOT = Path(__file__).resolve().parents[1]`.
- `kokoro_voice.main()`:
  - **Drain path on non-Windows.** `open_speaker()` does four things: `ensure_models()` (keeping today's download behaviour), then `ensure(PLUGIN_ROOT)`, which returns None if it fails (the drain then backs off 30 s), then builds `EngineSpeaker(lambda: EngineConnection.connect(), root / "play", play_file)`, then `warm_up()`.
  - **Windows** keeps `KokoroSpeaker`.
- `speak_text` on non-Windows keeps the lock, connects through `ensure`, and submits. With `--stream` it plays each `Pending.chunks` frame group as one WAV per sentence as they arrive; otherwise it plays the whole result.

- [ ] **Step 1: Write the failing tests.** Run a real stub engine through `ensure` with `VOICEOVER_ENGINE_STUB=1`, and stub `play_file` to record paths.
  - **Drain.** A queued "One. Two. Three." drains into three WAVs, each valid for `wave.open` at 24000 Hz, mono, 16-bit.
  - **Prefetch.** The second sentence's request was submitted before the first finished playing.
  - **Stop request.** A stop request mid-item kills playback, and the engine shows no request left active (`status()["requests"] == 0` within 0.5 s).
  - **Engine down.** `ensure` patched to False makes `open_speaker()` return None, and drain exits 0 with `spawned.json` pushed 27 s ahead.
  - **Warm while waiting.** During a live lease with an empty queue, `idle()` sends `warm` (seen in the engine's `status()["leases"] == 1`).
  - **Commands.** `speech.kokoro_command("--drain","/x")[0] == sys.executable` on Linux.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and check they pass.** Run `bash tests/run_all.sh`. Expected: ALL TESTS PASSED.
- [ ] **Step 5: Commit.** Message: `feat: narration drainers and one-shot speaker become engine clients`.

---

### Task 13: Kill switch split (M18)

**Files:**
- Modify: `tts/tts_controller.py` (`main()` accepts `stop` and `stop --all`)
- Test: `tests/test_engine_killswitch.py`

**Interfaces:**
- `tts_controller.py stop` behaves exactly as today.
- `stop --all` also calls `engine_client.EngineConnection.connect(timeout=0.5).stop()`, ignoring every error.

- [ ] **Step 1: Write the failing tests (T7 shape).**
  - Start a stub engine with a long request streaming on one client connection.
  - Run `tts_controller.stop_tts()` and `speech.stop_speech()`. The client's request still completes with `complete`.
  - Check that no pattern in `TTS_PROCESS_PATTERNS` or `UNIX_PATTERNS` matches `"python3 /p/tts/kokoro_engine.py"`.
  - `main` with `["stop","--all"]` makes the engine exit, with the client seeing `cancelled`.
- [ ] **Step 2: Run them and check they fail.** Expected: FAIL on `--all`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and check they pass.** Expected: all PASS.
- [ ] **Step 5: Commit.** Message: `feat: kill switch stops narration only; stop --all also stops the engine`.

---

### Task 14: INTERFACES.md, docs, release 1.6.0

**Files:**
- Modify: `INTERFACES.md` (a new section, "Shared engine (protocol 1)", plus updates to the `tts/` and `speech.py` sections), `README.md` (one section: "Sharing the engine with another program"), `CHANGELOG.md` (1.6.0), `.claude-plugin/plugin.json` (`"version": "1.6.0"`)

- [ ] **Step 1: Write the INTERFACES section** from the code as built. It covers:
  - transports and paths
  - auth
  - every op and event, with fields
  - frame rules, limits and error codes
  - the lifecycle rules (idle defaults, warm, quiet-moment exits, release, keep-off)
  - the launcher commands, the pointer contract (`current.json` fields and the version rule) and the systemd units
  - that byte order is little-endian, announced in `hello`
  - the my_agent probe numbers as a reference
- [ ] **Step 2: Check consistency.** Run `python3 tests/test_plugin_manifest.py && bash tests/run_all.sh`. Expected: ALL TESTS PASSED.
- [ ] **Step 3: Commit.** Message: `docs: protocol 1 in INTERFACES.md; release 1.6.0`.

---

### Task 15: Acceptance on the card (manual, the owner's machine)

No code. Record every number in `docs/superpowers/plans/2026-11-18-shared-kokoro-engine-acceptance.md`, then commit it.

- [ ] **Step 1: Set up.** Run `python3 scripts/setup_tts.py --share-engine 47320`. Then check:
  - `systemctl --user is-active voiceover-engine.socket` gives `active`.
  - `ss -ltn` shows only `127.0.0.1:47320`.
- [ ] **Step 2: T1 and T19.** Narrate in two projects at once while sending a `bf_emma` request from a script.
  - `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` shows exactly one plugin process, holding at most 1176 MiB.
  - After the idle time, the card list is back at the baseline.
- [ ] **Step 3: Latency.** Measure T6, T18, T20 (both variants) and T21 against 1.5.0, with a script using `EngineConnection`.
- [ ] **Step 4: Failure paths.** T9 (SIGKILL), T11 (install a bumped copy and remove the old folder), T12 (fill the card with a dummy allocation; force a CUDA failure), T13 (no Claude Code running: `voiceover-engine status`, then a request) and T14 (`voiceover-engine release --keep-off 60` during narration).
- [ ] **Step 5: Agent rerun.** Ask the my_agent session to rerun its boxed probe against the real engine, including first audio after a reconnect. Record its figures.
- [ ] **Step 6: Commit** the acceptance record.
