# Phonon Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Phonon as an opt-in, streaming neural TTS engine beside Kokoro, with an unused engine costing nothing.

**Architecture:** A new engine script `tts/phonon_voice.py` speaks the existing drain protocol from `tts/engine_common.py` and runs from its own uv project `tts-phonon/`. A worker thread streams audio chunks from `ptts` into a small audio sink (`tts/pcm_sink.py`). The shared drain loop gains two things: a drainer steps aside when another engine's speech arrives, and a speaker is told when the queue runs dry.

**Tech Stack:** Python stdlib for everything outside the engine; `ptts` 0.7.1+ and `sounddevice` inside `tts-phonon/` only; `uv` to run engines; plain-script tests under `tests/`.

**Spec:** `docs/superpowers/specs/2026-11-22-phonon-engine-design.md`. Read it first; this plan argues from it.

## Global Constraints

- `tts/*.py` must not import from `voiceover/`. `tts/engine_common.py` stays stdlib-only.
- Every engine script imports third-party modules (`ptts`, `sounddevice`) lazily, inside functions. Tests import the engine scripts under plain `python3` with no third-party packages installed.
- No code in this plan imports `numpy`. Audio chunks are handled through `len(chunk)` and `chunk.tobytes()` only.
- Model: `gradium/phonon-31m-en` only. Load with `quant="q8_0"`, `lang="en"`, after `ptts.set_num_threads(2)`.
- `tts_engine: "auto"` resolves exactly as it does today. Kokoro's code path and `tts/pyproject.toml` are not changed.
- `tts-phonon/pyproject.toml` lists no `kokoro-onnx`, `onnxruntime` or `soundfile`. `tts/pyproject.toml` and `tts-gpu/pyproject.toml` list no `ptts` or `sounddevice`.
- Tests: plain scripts using `tests/_harness.py` (`isolate()`, `check()`, `report()`), run by `tests/run_all.sh`. No real model, network, sound card or spawned engine. Stub third-party modules through `sys.modules`, as `tests/test_tts_device.py` does.
- Test gate (amended after the spike; it replaces every "Expected: `ALL TESTS PASSED`" on `bash tests/run_all.sh` below). The main checkout holds the user's git-ignored `.claude/voiceover.json`, which leaks into 8 existing checks through project scope; the harness and the existing test files are not changed for it. So, after each task's commit:
  1. Primary: `git clone` the local repo's `feature/phonon-engine` into the builder's scratch directory and run `bash tests/run_all.sh` there. Required: `ALL TESTS PASSED`. Never push from or to the clone; delete it after the run.
  2. Secondary: in the main checkout, the only failures allowed are the known 8 (in `tests/test_enqueue_speech.py`, and the 2 "enqueue fails" checks in `tests/test_narrator_hooks.py`).
  3. Every new test file passes in both places: it gives itself its own temp project directory, passes that as `cwd`, and never reads the spool or settings through the default cwd.
- Coding standards: single responsibility, DRY, small well-named methods.
- Release target: deferred (1.6.0 is already released upstream; see Task 9).
- Commits: on the feature branch the user names, and only if the user has allowed the builder to commit. Never push.

## Review Focus

1. A Phonon voice whose file cannot be fetched at narration time (offline, first use): the sentence is spoken in the bundled `default` voice, not skipped. Test in Task 5.
2. A stop request while a sentence is streaming: silence comes before the generator is closed, and the drain loop is not blocked by the close. Test in Task 5.
3. No working audio output at all (no PortAudio, no player on PATH): the engine opens no speaker, the existing 30-second back-off applies, and setup names what is missing. Tests in Tasks 4 and 5.
4. The engine is switched mid-turn under a live lease; and leftover items for another engine queued before a drainer started must not make it exit and respawn in a loop. Tests in Task 2.
5. A hand-edited `voice` holding the other engine's id, an unknown string, or a multi-word Phonon name ("Captain Silas Blackwater") passed through the queue and the one-shot command line. Tests in Tasks 3 and 5.

---

## File Structure

| File | Responsibility |
|---|---|
| `tts/engine_common.py` (modify) | Drain loop: hand-over rule, idle notification |
| `tts/pcm_sink.py` (create) | Play raw 24 kHz mono float32 audio as it arrives; two sinks and a chooser |
| `tts/phonon_voice.py` (create) | Phonon engine: model load, streaming speaker, CLI |
| `tts-phonon/pyproject.toml` (create) | Phonon's own uv environment |
| `voiceover/settings.py` (modify) | `phonon` engine, Phonon voice lists, engine-aware voice, model-present check |
| `voiceover/speech.py` (modify) | Phonon command line, dispatch and drain command |
| `voiceover/process_utils.py`, `tts/tts_controller.py` (modify) | Kill patterns and lock-owner name checks |
| `scripts/manage_voices.py`, `scripts/manage_settings.py`, `scripts/setup_tts.py` (modify) | Engine-generic catalogue, validation, setup |
| `commands/voice.md`, `commands/setup.md`, `README.md`, `INTERFACES.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (modify) | User-facing docs and version |

---

### Task 1: Spike — settle what is still unverified (throwaway)

No product code. Work in a scratch directory outside the repo, in a throwaway virtual environment. This task plays sound aloud.

**Produces:** a "Spike results" subsection appended to the spec's "Measured, and still unverified" section, one line per item below, each marked measured or not done. Later tasks read these values.

- [ ] **Step 1:** Install `ptts` and `sounddevice` in a scratch venv on macOS. Record whether `import sounddevice` works with no system package.
- [ ] **Step 2:** Record the exact Python call that selects a catalogue voice for one stream (expected `tts.stream(text, voice="Bryony")`; confirm the keyword), and the exception type raised for a voice that cannot be fetched.
- [ ] **Step 3:** Stream a 10-second sentence through `sounddevice.RawOutputStream(samplerate=24000, channels=1, dtype="float32")`, writing `chunk.tobytes()`. Listen: record whether it is gapless.
- [ ] **Step 4:** Mid-sentence, call the stream's `abort()` and then close the generator. Record whether silence is immediate, and whether `start()` after `abort()` lets the next sentence play on the same stream.
- [ ] **Step 5:** On Linux (or note "not done" if none is available): find which of `aplay`, `paplay`, `pw-cat`, `ffplay` plays raw float32 from standard input, with the exact arguments, and that killing the player silences at once.
- [ ] **Step 6:** With an empty Hugging Face cache, load with `quant="q8_0"`. Record which files download, and the cache path when `HF_HOME` is unset.
- [ ] **Step 7:** Record peak memory of a Kokoro drain and a Phonon drain speaking the same three sentences, if Kokoro is installed on the machine.
- [ ] **Step 8: Gate.** If audio cannot be made gapless, silence is not immediate, or a catalogue voice cannot be selected per stream, stop and report to the user. Otherwise write the results into the spec and continue. Where a result differs from a value this plan assumes (a keyword, a player's arguments), use the measured value and note it in the task that uses it.

---

### Task 2: Drain loop — hand over to another engine, and report idle

**Files:**
- Modify: `tts/engine_common.py` (the `drain` function and a new helper beside `_own_pending`)
- Test: `tests/test_engine_handover.py` (create)
- Modify: `INTERFACES.md` (the `tts/engine_common.py` section)

**Interfaces:**
- Produces: `foreign_pending(root, engine_names, since) -> bool` — true when a pending item names a real engine outside `engine_names` and its `created` is later than `since`.
- Produces: speakers may define an optional `idle()` method. `drain` calls it each time `next_item` finds nothing of this engine's to speak, before it waits on a lease or exits. It must return within 200 ms (amended after the spike: a PortAudio close takes about 115 ms on macOS), and it is called on every poll while the queue is dry, so a speaker's `idle()` must be near-free when there is nothing to release. Speakers without it are unaffected.
- Changes: in `drain`, when there is no own item and `foreign_pending(root, engine_names, started)` is true (`started` = the time `drain` claimed its lock), log `handover`, and return 0. The existing `finally` releases the lock. This check comes before the lease wait.

- [ ] **Step 1: Write the failing tests** in `tests/test_engine_handover.py`, following `tests/test_engine_lifecycle.py` for setup (`isolate()`, a project root, `spool.enqueue`, `lease.hold`, a collecting speaker).

```python
# 1. A warm drainer under a live lease exits when another engine's item arrives.
#    Hold a lease; start drain(ROOT, Collect, {"kokoro"}, poll_seconds=0.001) in a
#    thread; enqueue("Hello.", "phonon", "Bryony"); join with a 2s timeout.
check("a warm engine steps aside for another engine's speech", not thread.is_alive())
check("it released its lock", engine_common.lock_is_live(ROOT) is False)
check("it left the other engine's item in place", spool.pending_count() == 1)

# 2. Own items are spoken before stepping aside.
check("it finishes its own items first", spoken == ["Mine."], spoken)

# 3. A foreign item queued BEFORE the drainer started does not make it leave.
#    enqueue a "phonon" item, then hold a lease, start a kokoro drain, wait 0.2s.
check("an older foreign item does not evict the engine", thread.is_alive())

# 4. A lease alone still keeps the engine warm (unchanged behaviour).
check("a lease with an empty queue keeps the engine", thread.is_alive())

# 5. idle() is called when the queue runs dry, and a speaker without idle() works.
check("the speaker is told the queue ran dry", idle_calls >= 1, idle_calls)
```

Release the lease at the end of cases 3 and 4 so each thread exits.

- [ ] **Step 2:** Run `python3 tests/test_engine_handover.py`. Expected: FAIL on cases 1, 2 and 5.
- [ ] **Step 3:** Implement `foreign_pending` and the two `drain` changes described under Interfaces.
- [ ] **Step 4:** Run `python3 tests/test_engine_handover.py` and `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`.
- [ ] **Step 5:** Check the Stop hook's path: after its flush poll, does anything call `speech.ensure_drainer` again? If the last sentence of a turn can be queued for a new engine with no later hook to start that engine, stop and report it to the user with the file and line.
- [ ] **Step 6:** Update `INTERFACES.md`, then commit: `feat: a drainer hands over to another engine and reports idle`.

---

### Task 2b: A newcomer engine waits for one that is handing over

Added after Task 2's Step 5 found that a turn's closing message, queued for a newly chosen engine at `hooks/stop.py:79`, is left unspoken: `hooks/stop.py:84` sees the old engine's live lock and starts nothing, and no later hook in the turn starts the new one.

**Files:** `tts/engine_common.py`, `voiceover/speech.py`, `tests/test_engine_handover.py` (extend), `INTERFACES.md`, the spec.

- A drainer's lock JSON gains `"engines"` (sorted engine names), written at every claim and kept by every expiry refresh. One-shot and older locks have none and behave as today.
- `speech.ensure_drainer`: where a live lock with a running owner made it return False, it spawns the resolved engine when the lock records engines, the resolved engine is not among them, and the spool has items. The `spawned.json` back-off still applies; no waiting in the hook.
- `engine_common.drain`: when the first claim fails and the holder records engines disjoint from ours and we have pending items, poll every `poll_seconds` until the claim succeeds, none of our items remain, or `QUEUE_MAX_AGE_SECONDS` pass. Load nothing while waiting; refresh the spawn marker about once a second. A holder with our engine or with no recorded engines: return 0 as today.
- Task 2 follow-up: a speaker's `idle()` that raises is logged and the drain carries on.
- Tests: the finale case end to end in-process; a second `ensure_drainer` spawns nothing while one newcomer waits; a waiting newcomer whose items disappear exits without loading; a second drain for the same engine exits at once; a lock without `"engines"` is left alone; `"engines"` survives an expiry refresh; `open_speaker` only after the claim; an `idle()` that raises does not end the drain. `test_engine_lifecycle.py`, `test_lock_ownership.py` and `test_drain.py` pass untouched.
- Commit: `feat: a newcomer engine waits for one that is handing over`.

---

### Task 3: Settings — the phonon engine and engine-aware voices

**Files:**
- Modify: `voiceover/settings.py`
- Test: `tests/test_phonon_settings.py` (create)
- Modify: `INTERFACES.md` (the `voiceover/settings.py` section)

**Interfaces — produces:**
- `PHONON_VOICES`: the 25 standard names, in this order: Zoey, Juniper, Everly, Harper, Hadley, Delia, Easton, Micah, Trevor, Russell, Preston, Miles, Maeve, Freya, Pippa, Elodie-Rose, Imogen, Lottie, Alfie, Hugh, Edwin, Spencer, Toby, Teddy, Bryony.
- `PHONON_CHARACTER_VOICES`: Captain Silas Blackwater, Morwenna of the Deep, Old Mortlake, Azgorath, Fennick the Young Elf, The Thing Under the Stairs, Vyrmathrax the Ancient, Grub the Ogre, Master Aldric, The Snow Queen, Sir Bramwell, Fairy Godmother.
- `PHONON_DEFAULT_VOICE = "default"`.
- `is_phonon_voice(name) -> bool` — true for any of the 37 names or `"default"`, exact case.
- `phonon_model_present() -> bool` — true when `<hub>/models--gradium--phonon-31m-en/snapshots/*/model.q8.gguf` exists, where `<hub>` is `$HF_HOME/hub` if `HF_HOME` is set, else `~/.cache/huggingface/hub` (or the path Task 1 measured). Never raises.
- `resolve_engine(cwd)` accepts `"phonon"` as an explicit value. `auto` is unchanged.
- `get_voice(cwd)` — rules, with the hash pick unchanged in method but choosing from the resolved engine's list:
  - engine `phonon`: the explicit voice if `is_phonon_voice`; otherwise the hash pick from `PHONON_VOICES`.
  - any other engine: the explicit voice unless `is_phonon_voice` (then the hash pick from `KOKORO_VOICES`); with no explicit voice, the hash pick from `KOKORO_VOICES` as today.
- `DEFAULTS["tts_engine"]` comment lists `phonon`.

- [ ] **Step 1: Write the failing tests** in `tests/test_phonon_settings.py`.

```python
check("phonon is an explicit engine", engine_for("phonon") == "phonon")
check("auto is unchanged on this platform", engine_for("auto") == auto_before)
check("25 standard and 12 character voices",
      (len(settings.PHONON_VOICES), len(settings.PHONON_CHARACTER_VOICES)) == (25, 12))
check("no Phonon name collides with a Kokoro friendly name", not collisions, collisions)
check("an explicit Phonon voice wins under phonon", voice_for("phonon", "Bryony") == "Bryony")
check("a character voice may be chosen explicitly",
      voice_for("phonon", "Captain Silas Blackwater") == "Captain Silas Blackwater")
check("a Kokoro id under phonon falls back to a standard Phonon voice",
      voice_for("phonon", "af_sky") in settings.PHONON_VOICES)
check("the automatic Phonon pick is never a character voice",
      all(voice_for("phonon", None, root) in settings.PHONON_VOICES for root in fifty_roots))
check("the automatic Phonon pick is stable per project", pick_once == pick_again)
check("Kokoro still passes any explicit string through", voice_for("kokoro", "zf_custom") == "zf_custom")
check("a Phonon name under kokoro falls back to a Kokoro voice",
      voice_for("kokoro", "Bryony") in settings.KOKORO_VOICES)
check("the Kokoro automatic pick is unchanged", voice_for("kokoro", None, root) == kokoro_pick_before)
check("no model, not present", settings.phonon_model_present() is False)
check("the 8-bit weights file makes it present", settings.phonon_model_present() is True)
```

`collisions` compares lower-cased, hyphenated Phonon names with the friendly names in `scripts/manage_voices.py`. `kokoro_pick_before` is computed with the same sha1 formula the current code uses. For the model checks, set `HF_HOME` to a temp directory and create the snapshot file.

- [ ] **Step 2:** Run `python3 tests/test_phonon_settings.py`. Expected: FAIL.
- [ ] **Step 3:** Implement the interfaces above.
- [ ] **Step 4:** Run the file and `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`.
- [ ] **Step 5:** Update `INTERFACES.md`, then commit: `feat: phonon engine setting and engine-aware voice resolution`.

---

### Task 4: The audio sink

**Files:**
- Create: `tts/pcm_sink.py`
- Test: `tests/test_pcm_sink.py` (create)
- Modify: `INTERFACES.md` (the `tts/` section)

**Interfaces — produces** (stdlib only at import; `sounddevice` imported inside `SoundDeviceSink.__init__`):
- `SAMPLE_RATE = 24000`.
- `class Sink` with:
  - `write(chunk) -> None` — plays `chunk.tobytes()`; opens the output on the first write.
  - `abort() -> None` — silence now, discard buffered audio; the next `write` plays again.
  - `close() -> None` — let written audio finish, release the device; returns within 200 ms (amended after the spike). Idempotent and near-free when already closed: `drain` calls `idle()`, and so `close()`, on every poll while the queue is dry. The next `write` after a close re-opens.
  - `remaining() -> float` — seconds of written audio not yet played; `abort` resets it to zero.
- `class SoundDeviceSink(Sink)` — amended after the spike, which measured that a blocking `write()` on one thread with `abort()` from another crashes the process (bus error). Callback-fed instead: a `sounddevice.RawOutputStream(samplerate=24000, channels=1, dtype="float32", latency="low", blocksize=480, callback=...)`. `write` appends `chunk.tobytes()` to a buffer the sink owns, under a lock, and starts the stream if it is not open; the callback copies from that buffer and fills the rest with zeros when it runs short. `abort` empties the buffer (the stream stays open; the next `write` plays on it). `remaining()` = buffered samples / `SAMPLE_RATE`. `close` calls the stream's `stop()` (not `abort()`, so a tail still in the device plays out) and then `close()`.
- `class PipeSink(Sink)` — a player process fed on standard input. `abort` kills it; `close` closes its standard input without waiting. `remaining()` by wall clock: each `write` extends an end time by `len(chunk) / SAMPLE_RATE` from `max(now, end)`.
- `player_command() -> list | None` — the first available of, in order: `aplay -q -t raw -f FLOAT_LE -r 24000 -c 1 -`, `paplay --raw --format=float32le --rate=24000 --channels=1`, `pw-cat --playback --raw --format f32 --rate 24000 --channels 1 -`, `ffplay -f f32le -ar 24000 -nodisp -autoexit -loglevel error -i -`. The ffplay line is the one Task 1 measured (ffplay 9 rejects `-ac 1`; raw input defaults to mono). The `aplay`, `paplay` and `pw-cat` lines are untested: say so in a comment and in `INTERFACES.md`.
- `open_sink() -> Sink | None` — `SoundDeviceSink` when it constructs, else `PipeSink` when `player_command()` is not None, else `None`. Never raises.

- [ ] **Step 1: Write the failing tests** in `tests/test_pcm_sink.py`. Use a fake chunk class with `__len__` and `tobytes()`, a stub `sounddevice` module in `sys.modules` whose stream records calls, and a `PipeSink` pointed at `[sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]`.

```python
check("the device opens on the first write, not before", stream_opened_before == 0 and stream_opened_after == 1)
check("a chunk is written as raw bytes", written == [b"\x00" * 7680])
check("remaining counts written audio", 0.07 < sink.remaining() <= 0.08)   # one 1,920-sample chunk
check("abort silences and resets remaining", callback_output_after_abort == b"\x00" * 1920 and sink.remaining() == 0.0)
check("writing after abort plays on the same open stream", opened == 1 and callback_output_after_write != b"\x00" * 1920)
check("close stops the stream rather than aborting it", "stop" in calls and "abort" not in calls)
check("close releases the device and a later write re-opens it", opened == 2)
check("a second close does not re-close or re-open", (closes_after_second, opened_after_second) == (1, 1))
check("the pipe sink feeds the player's stdin", player_received == 7680)
check("aborting the pipe sink kills the player", player.poll() is not None)
check("closing the pipe sink returns at once", close_seconds < 0.1)
check("PortAudio missing falls back to the piped player", isinstance(pcm_sink.open_sink(), pcm_sink.PipeSink))
check("no device and no player gives None", pcm_sink.open_sink() is None)
```

For the last two, make the stub `sounddevice` import raise `OSError("PortAudio library not found")` and patch `player_command`.

- [ ] **Step 2:** Run `python3 tests/test_pcm_sink.py`. Expected: FAIL (no module).
- [ ] **Step 3:** Implement `tts/pcm_sink.py`.
- [ ] **Step 4:** Run the file. Expected: PASS.
- [ ] **Step 5:** Update `INTERFACES.md`, then commit: `feat: streaming audio sink for raw PCM`.

---

### Task 5: The Phonon engine

**Files:**
- Create: `tts-phonon/pyproject.toml`
- Create: `tts/phonon_voice.py`
- Test: `tests/test_phonon_engine.py` (create)
- Modify: `INTERFACES.md` (the `tts/` section)

**Interfaces:**
- Consumes: `engine_common.drain`, `drain_root`, `Playback`, `DryRunSpeaker`, `try_claim_lock`, `remove_tts_lock`, `data_dir`, `engine_log`; `pcm_sink.open_sink`, `Sink`.
- Produces — `tts-phonon/pyproject.toml`: project `claude-voiceover-tts-phonon`, `requires-python = ">=3.10"`, dependencies `ptts>=0.7.1` and `sounddevice>=0.5`, the same build-system and setuptools blocks as `tts/pyproject.toml`.
- Produces — `tts/phonon_voice.py`:
  - Constants: `MODEL_REPO = "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"` (amended after the spike: an unpinned name checks the network on every load, 17 s when it is unreachable; pinned, 0.41 s online or offline), `QUANT = "q8_0"`, `DEVICE = "cpu"`, `THREADS = 2`, `FALLBACK_VOICE = "default"`, `WARM_UP_TEXT = "Ready."`.
  - `load_model()` — `ptts.set_num_threads(THREADS)` then `ptts.TTS(MODEL_REPO, lang="en", quant=QUANT, device=DEVICE)`. Metal is out of scope and not exposed.
  - `class PhononSpeaker` — `__init__(self, tts, sink)`; `warm_up()` renders `WARM_UP_TEXT` and discards the audio; `prefetch(sentence, voice)` does nothing; `begin(sentence, voice, upcoming=None) -> PhononPlayback`; `idle()` calls `sink.close()`.
  - `class PhononPlayback(Playback)`:
    - One worker thread per speaker runs sentences in order. For a sentence it opens `tts.stream(sentence, voice=...)`, writes each chunk to the sink unless cancelled, and always closes the stream itself.
    - A voice that cannot be loaded: the `stream()` call itself raises, before any chunk (measured: `LookupError` for an unknown or offline-uncached voice, `OSError` when a fetch fails, after about 2.6 s). Catch both, log it with `engine_log("voice", ...)` and render the sentence with `FALLBACK_VOICE`. Remember the failed voice for the life of the process and go straight to `FALLBACK_VOICE` for it afterwards, so an offline machine pays the fetch delay once, not per sentence.
    - Voice names are stored and passed in their exact catalogue case; do not rely on `ptts` matching names case-insensitively.
    - `started()` — true once a chunk has been written.
    - `poll()` — re-raises a generation error; otherwise true when the worker has finished the sentence and `sink.remaining() <= 0`.
    - `kill()` — sets the cancel flag, calls `sink.abort()`, and returns without waiting for the worker.
  - CLI, mirroring `kokoro_voice.py`: `--drain <project dir>` (engine names `{"phonon"}`; `open_speaker` returns `None` when `open_sink()` is `None` or the model fails to load, logging why); `--voice <name> --stream <text>` and `<text> --voice <name>` for one utterance under the one-shot lock; `--install` loads the model and exits 0, or 1 with the error on stderr; `--fetch-voice <name>` loads the model, renders one word in that voice, exits 0, or 1 if the voice cannot be loaded. `VOICEOVER_DRY_RUN` behaves as in `kokoro_voice.py`.

- [ ] **Step 1: Write the failing tests** in `tests/test_phonon_engine.py`. Stub `ptts` in `sys.modules`: `set_num_threads` records its argument; `TTS(...)` records its arguments; `stream(text, voice=None)` returns an object that yields fake chunks, records `close()`, sleeps 0.15 s inside `close()`, and raises `OSError("cannot fetch")` for the voice `"Missing"`. Use a recording fake sink whose `remaining()` the test controls.

```python
check("the model loads with 2 threads and 8-bit weights",
      (threads, tts_kwargs) == ([2], {"lang": "en", "quant": "q8_0", "device": "cpu"}))
check("chunks reach the sink as they are generated", sink.written == 3)
check("a sentence is not done until its audio has played out", playback.poll() is False)   # remaining() > 0
check("then it is", playback.poll() is True)                                                # remaining() == 0
check("the voice name is passed through, spaces and all",
      streams[-1].voice == "Captain Silas Blackwater")
check("an unfetchable voice is spoken in the default voice",
      [s.voice for s in streams[-2:]] == ["Missing", "default"] and sink.written > 0)
check("an unfetchable voice is tried once per process, not per sentence",
      stream_voices_for_two_sentences == ["Missing", "default", "default"])
check("the model loads on the CPU from the pinned revision",
      tts_args[0].endswith("@92741c5b07d085ff840e26fea8ce43110b87852f") and tts_device == "cpu")
check("kill silences before the generator is closed", events[:2] == ["abort", "close"])
check("kill does not wait for the generator to close", kill_seconds < 0.05)
check("a killed sentence writes nothing more", sink.written == written_at_kill)
check("the next sentence plays after a kill", next_playback_written > 0)
check("a generation error surfaces from poll", raised_runtime_error)
check("idle releases the audio device", sink.closed == 1)
check("warm-up renders one sentence and plays nothing", warm_streams == 1 and sink.written == 0)
check("no audio output means no speaker", open_speaker_result is None)
check("a dry-run drain prints the sentence and loads nothing", "[voiceover] Hello." in stderr and not tts_built)
check("the one-shot form parses a multi-word voice",
      phonon_voice.parse_args(["--voice", "Old Mortlake", "--stream", "Hi."]) == ("Old Mortlake", "Hi.", True))
```

Dependency isolation, in the same file:

```python
check("the Phonon environment carries nothing of Kokoro's",
      not {"kokoro-onnx", "onnxruntime", "soundfile"} & names("tts-phonon/pyproject.toml"))
check("the Kokoro environments carry nothing of Phonon's",
      not {"ptts", "sounddevice"} & (names("tts/pyproject.toml") | names("tts-gpu/pyproject.toml")))
```

A drain-level test: enqueue two items for `"phonon"`, run `engine_common.drain(ROOT, lambda: speaker, {"phonon"})` with the stubbed speaker, and check both were streamed in order and the sink was closed once the queue ran dry.

- [ ] **Step 2:** Run `python3 tests/test_phonon_engine.py`. Expected: FAIL (no module).
- [ ] **Step 3:** Create `tts-phonon/pyproject.toml` and implement `tts/phonon_voice.py`. Share the argument parsing with `kokoro_voice.py` only if it can move to `engine_common.py` without changing Kokoro's legacy bare form; otherwise keep Phonon's parser small and separate.
- [ ] **Step 4:** Run the file and `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`.
- [ ] **Step 5:** Update `INTERFACES.md`, then commit: `feat: Phonon streaming engine in its own environment`.

---

### Task 6: Dispatch and the kill switch

**Files:**
- Modify: `voiceover/speech.py`, `voiceover/process_utils.py`, `tts/tts_controller.py`
- Test: `tests/test_phonon_dispatch.py` (create); `tests/test_stop_scope.py` (extend)
- Modify: `INTERFACES.md`

**Interfaces:**
- Produces: `speech.phonon_command(*args) -> list` — `["uv", "run", "--project", <plugin>/tts-phonon, <plugin>/tts/phonon_voice.py, *args]`.
- Changes: `_drain_command("phonon", root)` returns `phonon_command("--drain", str(root))`. `_dispatch` sends `phonon_command("--voice", get_voice(cwd), "--stream", message)`. Kokoro and Phonon go through one lookup of engine name to command builder rather than a second `if` chain.
- Changes: `enqueue_speech` already takes the voice from `get_voice(cwd)` for non-macOS engines; confirm a Phonon item carries `engine: "phonon"` and a Phonon voice name.
- Changes: `process_utils.TTS_PROCESS_PATTERNS` gains `"phonon_voice.py --voice"`. The lock-owner name check in `process_utils._kill_lock_owner_group` and in `tts_controller._kill_lock_owner_groups` accepts `phonon_voice.py`; keep the list of engine script names in one place per file. `tts_controller.UNIX_PATTERNS` and `WINDOWS_PATTERNS` gain `phonon_voice\.py`.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_phonon_dispatch.py
check("the Phonon command runs from its own uv project",
      cmd[:4] == ["uv", "run", "--project", str(REPO_ROOT / "tts-phonon")]
      and cmd[4] == str(REPO_ROOT / "tts" / "phonon_voice.py"))
check("the drain command names the project directory", cmd[-2:] == ["--drain", str(root)])
check("a queued Phonon item carries its engine and voice",
      (item["engine"], item["voice"]) == ("phonon", "Bryony"))
check("ensure_drainer starts the Phonon engine for a Phonon project", spawned[0][5:] == ["--drain", str(root)])
check("a Kokoro project still gets the Kokoro command", "kokoro_voice.py" in kokoro_cmd[4])
```

Capture spawns by replacing `speech._spawn_detached`. In `tests/test_stop_scope.py`, add the Phonon rows to the existing tables:

```python
check("a Phonon drainer survives the one-shot interrupt", not unix_kills("... phonon_voice.py --drain /p"))
check("a Phonon one-shot is stopped by it", unix_kills("... phonon_voice.py --voice Bryony --stream Hi."))
check("the kill switch stops a Phonon drainer", controller_kills("... phonon_voice.py --drain /p"))
check("the kill switch recognises a Phonon lock owner", "phonon_voice.py" in controller_engine_names)
```

- [ ] **Step 2:** Run both files. Expected: FAIL.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`.
- [ ] **Step 5:** Update `INTERFACES.md`, then commit: `feat: dispatch and stop the Phonon engine`.

---

### Task 7: Voice and settings commands

**Files:**
- Modify: `scripts/manage_voices.py`, `scripts/manage_settings.py`, `commands/voice.md`
- Test: `tests/test_phonon_voices_cli.py` (create)

**Interfaces — changes:**
- `manage_voices.VOICES` gains one entry per Phonon voice, built from the settings lists: key = the name lower-cased with spaces replaced by hyphens; `engine: "phonon"`; `voice:` the exact Phonon name; `label:` `"<Name> (Phonon)"` or `"<Name> (Phonon character)"`.
- `voice_availability(spec)` for Phonon: `ready` when uv is on PATH and `settings.phonon_model_present()`; otherwise the same wording as Kokoro's, naming `/voiceover:setup`.
- `cmd_list` prints the voices grouped under engine headings.
- `speak_sample` uses one engine-to-command lookup covering Kokoro and Phonon.
- `cmd_set` for a Phonon voice, outside dry run and with Phonon ready: runs `phonon_command("--fetch-voice", name)` first. On failure it prints that the voice could not be downloaded, saves nothing, and returns 1. When Phonon is not set up yet it saves and prints the existing "will work once setup completes" note.
- `cmd_recommend`: a Phonon voice when Phonon is ready (placeholder: `zoey`, the first standard voice, until the user names one), else today's logic. When Phonon is not installed it adds one line mentioning it.
- `manage_settings.VALID_ENGINES` gains `phonon`; `normalize("voice", v)` accepts `None`, a Kokoro id or any name `settings.is_phonon_voice` accepts; `cmd_info` prints a Phonon model line beside the Kokoro one.
- `commands/voice.md`: the description names the three engine families instead of listing every voice; the reference section points to `manage_voices.py list`.

- [ ] **Step 1: Write the failing tests** in `tests/test_phonon_voices_cli.py`.

```python
check("all 37 Phonon voices are in the catalogue", phonon_entries == 37)
check("Kokoro's thirteen names keep their meaning", manage_voices.VOICES["echo"]["voice"] == "am_echo")
check("a character voice has a hyphenated friendly name",
      manage_voices.VOICES["captain-silas-blackwater"]["voice"] == "Captain Silas Blackwater")
check("setting a Phonon voice sets engine and voice together",
      (settings.get_setting("tts_engine"), settings.get_setting("voice")) == ("phonon", "Bryony"))
check("setting a Kokoro voice afterwards switches back", settings.get_setting("tts_engine") == "kokoro")
check("a voice that cannot be fetched is not saved", code == 1 and settings.get_setting("voice") == before)
check("the list groups voices by engine", "Phonon" in listing and "Kokoro" in listing)
check("recommend prefers Phonon once it is installed", "zoey" in recommended.lower())
check("recommend is unchanged without Phonon", recommended_without == recommended_before)
check("manage_settings accepts the phonon engine", manage_settings.normalize("tts_engine", "phonon") == "phonon")
check("manage_settings accepts a Phonon voice", manage_settings.normalize("voice", "Bryony") == "Bryony")
```

Patch `manage_voices.kokoro_ready`, a new `phonon_ready`, and the fetch call; the harness already sets dry run, so the fetch test clears `VOICEOVER_DRY_RUN` for that case only.

- [ ] **Step 2:** Run the file. Expected: FAIL.
- [ ] **Step 3:** Implement the changes and update `commands/voice.md`.
- [ ] **Step 4:** Run `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`.
- [ ] **Step 5:** Commit: `feat: Phonon voices in the voice and settings commands`.

---

### Task 8: Setup

**Files:**
- Modify: `scripts/setup_tts.py`, `commands/setup.md`
- Test: `tests/test_phonon_setup.py` (create)

**Interfaces — changes:**
- `setup_tts.py --download-phonon` runs `phonon_command("--install")` and reports success or the failure. `--download` keeps meaning Kokoro.
- `print_status()` gains a "Phonon neural voices (37 voices, local and free, about 65MB)" block before the Kokoro block: model present or not, and, on Linux, whether `sounddevice`'s PortAudio or a piped player is available (`shutil.which` over the player names in Task 4). When Phonon's model is present but the resolved engine is not `phonon`, print: `Phonon is installed but not selected. Pick a voice with /voiceover:voice.`
- `speak_test()` uses the same engine-to-command lookup as `manage_voices.speak_sample`; put that lookup in one place both scripts import rather than a third copy.
- `commands/setup.md`: offer Phonon first (about 65MB), then Kokoro (about 300MB); state that installing does not change the current voice; the voice step lets the user pick.

- [ ] **Step 1: Write the failing tests** in `tests/test_phonon_setup.py`.

```python
check("the status reports Phonon before Kokoro", out.index("Phonon") < out.index("Kokoro neural voices"))
check("installed but not selected is said plainly",
      "Phonon is installed but not selected" in out)
check("--download-phonon runs the engine's installer", ran[0][-1] == "--install")
check("--download still means Kokoro", kokoro_download_called and not phonon_install_called)
check("installing Phonon selects nothing", settings.get_setting("tts_engine") == engine_before)
check("a missing Linux player is named", "aplay" in out_linux_no_player)
```

- [ ] **Step 2:** Run the file. Expected: FAIL.
- [ ] **Step 3:** Implement the changes and update `commands/setup.md`.
- [ ] **Step 4:** Run `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`.
- [ ] **Step 5:** Commit: `feat: Phonon in first-run setup`.

---

### Task 9: Docs, version and live verification

Amended 2026-11-22: This branch was cut from 1.5.0 (2658e91) and has not been merged with 1.6.0 (the shared Kokoro engine). The version step is deferred: the docs go under "Unreleased", neither
manifest's version changes, and the number and the release are the user's. The live checks run
for Phonon only, on this base; check 5 and the Kokoro side of the lag comparison wait for the merge.

**Files:**
- Modify: `README.md`, `INTERFACES.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`

- [ ] **Step 1:** README: Phonon in the feature list, voices section, requirements (uv; about 65MB; on Linux an audio player or PortAudio), and a short "which engine" note. State plainly that Phonon is opt-in and CPU only, that the bundled model is English, and that the speed and quality figures are Gradium's plus one laptop measurement.
- [ ] **Step 2:** `INTERFACES.md`: confirm every interface added in Tasks 2 to 8 is recorded. `CHANGELOG.md`: a 1.6.0 entry. Both manifests: version `1.6.0`; add Phonon to the description and `phonon` to the keywords.
- [ ] **Step 3:** Run `bash tests/run_all.sh`. Expected: `ALL TESTS PASSED`. Paste the tail of the output into the hand-off.
- [ ] **Step 4: Live checks on this machine**, each reported as passed, failed or not done, with what was heard:
  1. `/voiceover:setup` installs Phonon; before it, `tts-phonon/.venv` and the model folder do not exist.
  2. Pick `bryony`; a sample plays. Narrate a turn at the `narrator` level: speech starts before a long sentence has finished rendering, and sentences join without gaps or clicks.
  3. Send a new prompt mid-sentence: silence is immediate.
  4. After the turn ends, `pgrep -f phonon_voice.py` finds nothing.
  5. Mid-turn, pick a Kokoro voice: the next message is spoken by Kokoro, and no Phonon process remains. Then the reverse.
  6. Two projects at once, one on each engine: both narrate.
  7. With debug logging on, compare first-sound lag for Phonon and Kokoro on the same three messages.
- [ ] **Step 5:** Commit: `chore: release 1.6.0 (Phonon engine)`. Do not tag, push or publish; hand back to the user with the live-check results and the two items still open from the spec: Windows has not been run, and the recommended Phonon voice is the placeholder `zoey` until the user chooses.
