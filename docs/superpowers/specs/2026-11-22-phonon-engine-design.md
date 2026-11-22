# Phonon Engine — Design

Date: 2026-11-22
Status: approved by the user 2026-11-22.
Target version: 1.6.0
Builds on: `2026-09-10-narration-queue-design.md` and
`2026-10-02-low-latency-narration-design.md`. The spool, the lock, the lease
and the jump rule stand unchanged. The drain loop gains one rule, described
under "Switching engine while one is held warm".

## Intent

Add Phonon (Gradium's on-device TTS, Python package `ptts`) as a second neural
engine beside Kokoro.

Why, in the user's words: Phonon "takes less resources than Kokoro and has
premium voices", so it is the better choice on most machines. Kokoro stays,
because the user loves some of its voices.

## Goals

1. A user can pick a Phonon voice and hear narration through it, at every
   interaction level, on macOS, Linux and Windows.
2. Phonon audio is **streamed**: sound starts while the sentence is still
   being generated, not after the whole sentence has rendered.
3. **An engine that is not in use costs nothing on that machine.** Stated so
   it can be tested:
   - No process: no Phonon process exists unless some project's resolved
     engine is `phonon` and it has speech queued or a live lease. Same for
     Kokoro. (This is already how engines live and die; it must stay true.)
   - No install: the Phonon Python environment is not built, and the Phonon
     model is not downloaded, until Phonon is first selected or set up. A
     Phonon-only user never builds the Kokoro environment or downloads the
     Kokoro models.
   - No shared dependencies: neither engine's environment contains the
     other's packages.
   - No held device: an engine that is waiting warm on a lease holds no open
     audio output.
4. Both engines can be installed on one machine, and two projects can narrate
   at once with different engines.
5. Existing users see no change on upgrade.

## Non-goals

- Changing what `tts_engine: "auto"` resolves to. Phonon is opt-in (the user
  chose this: "3 sounds good if it's simple and won't cause issues").
- The 40M-parameter and multilingual Phonon models. They are CC BY-NC and
  gated behind a Hugging Face login. Only `gradium/phonon-31m-en` (Apache 2.0,
  English) ships.
- Phonon on a GPU. `tts_device` stays a Kokoro-only setting.
- Voice cloning or user-supplied voice files (`add_voice`).
- Streaming for Kokoro. Its sentence-at-a-time playback is unchanged.

## Anti-goals

- Any always-on Phonon process, server or daemon.
- One process or one environment that carries both engines.
- A Kokoro user's narration changing voice, engine or latency after upgrading.
- A stop request that takes longer to silence Phonon than it takes to silence
  Kokoro today.

## Constraints

- `tts/` engine scripts must not import from `voiceover/`. Shared engine code
  lives in `tts/engine_common.py`, which is stdlib-only.
- Hooks never wait on an engine; engines are spawned detached.
- Tests are plain scripts under `tests/` (no framework), run by
  `tests/run_all.sh`, and must not need the real model, the network or a
  sound card. `tests/test_tts_device.py` shows the house style for stubbing
  an engine's third-party modules.
- The user's coding standards: single responsibility, DRY, small well-named
  methods.

## Approaches

1. **A sibling engine script in its own uv project** (chosen). A new
   `tts/phonon_voice.py` speaks the existing drain protocol through
   `engine_common.drain`, and runs from a new `tts-phonon/` uv project, the
   same way the GPU build runs `tts/kokoro_voice.py` from `tts-gpu/`.
   Trade-off: a second engine script to maintain. In return the idle rule
   holds by construction, and nothing in Kokoro's path changes.
2. **One neural engine script that loads either model.** Rejected: it merges
   the two dependency sets into one environment and makes it easy to hold
   both models in memory, which breaks goal 3.
3. **Phonon's bundled WebSocket / OpenAI-compatible server.** Rejected: an
   always-on process, which breaks goal 3.

The user picked approach 1, and then overruled the first playback proposal
(render each sentence to a wav and play the file): "there has to be a way to
both enable idle and streaming support." So approach 1 is built with true
streaming, below.

## Design

### Selecting the engine

- `tts_engine` gains the value `phonon`. `auto` is untouched.
- The `voice` setting keeps holding one engine-specific voice id. When
  `tts_engine` is `phonon` it holds a Phonon voice name.
- `settings.get_voice(cwd)` becomes engine-aware, and the hash pick chooses
  among **the resolved engine's** voices.
  - Under Kokoro nothing changes: any explicit voice string is passed
    through as it is today, including ids outside the thirteen listed ones
    that a hand-edited settings file may hold. The one exception is a string
    that is a known Phonon voice name, which falls back to the hash pick.
  - Under Phonon an explicit voice wins only when it is a known Phonon voice
    name; anything else falls back to the hash pick among Phonon voices.
- Picking a voice by friendly name sets engine and voice together, as today.
  So choosing a Kokoro voice switches that scope to Kokoro, and choosing a
  Phonon voice switches it to Phonon. Project scope still overrides global.

### Switching engine while one is held warm

This is the one change to the shared drain loop, and it applies to every
engine (Kokoro, Phonon, macOS `say`).

Today a drainer holds its project's lock for as long as a session's lease is
live, which can be up to ten minutes, and it skips items tagged for another
engine. The hooks start no engine while that lock is live. So a user who
picks a voice on another engine mid-turn would hear silence until the lease
ends, with the old engine still loaded, and items older than five minutes
would be dropped. That breaks goal 3 and makes the first thing heard after
choosing Phonon nothing at all.

The fix: a drainer with nothing of its own to speak, that sees pending items
owned by a **different** engine, steps aside. It releases the lock and exits,
even under a live lease, so the next hook starts the right engine. It never
abandons a sentence it is speaking, and it never touches the foreign items.
The rule needs its own tests, including that a lease alone (no foreign items)
still keeps the engine warm as before.

### The engine process

- `tts/phonon_voice.py`, run as
  `uv run --project <plugin>/tts-phonon <plugin>/tts/phonon_voice.py ...`.
- Same two entry forms as Kokoro: `--drain <project dir>` for the narrator
  queue, and `--voice <name> --stream <text>` for the one-shot speaker. Same
  `VOICEOVER_DRY_RUN` behaviour.
- It drains only items whose engine is `phonon`, under the project's existing
  lock, lease and `MAX_ENGINES` cap. It loads the model once, after the lock
  is claimed, and exits when the queue is dry and no lease is live.
- `tts-phonon/pyproject.toml` depends on `ptts` (0.7.1 or newer) and
  `sounddevice`, and on nothing from Kokoro's world. `ptts` brings `numpy`
  with it and nothing else (measured).
- The model loads with the 8-bit weights (`quant="q8_0"`) and the thread
  count is set explicitly to 2. Measured: the package default of every
  logical CPU is the slowest setting and collapses when the machine is busy;
  2 threads renders about eight times faster than real time, holds a 26 to
  31 ms first chunk, and held up best under load. The 8-bit weights load at
  139 MiB against 289 MiB.
- After loading, the engine renders one short throwaway sentence. Measured:
  the first sentence in a process takes 130 to 270 ms to its first chunk and
  every later one 13 to 31 ms.

### Streaming playback

- Each sentence is one `Playback`, as the drain loop expects. A worker thread
  pulls audio chunks from `ptts`'s streaming generator and writes them to an
  audio output stream as they arrive.
- Chunks are mono float32 numpy arrays at 24 kHz, in multiples of 1,920
  samples (80 ms) (measured).
- `poll()` is true once generation has finished and the written audio has
  played out. `started()` is true once the first chunk has been written. A
  generation error surfaces from `poll()`, like Kokoro's.
- `kill()` silences the audio output **first**, discarding buffered audio,
  and only then stops generation. Measured: closing the generator blocks for
  90 to 220 ms until generation has really stopped, so it must not sit
  between the stop request and silence, and it must not block the drain
  loop. The worker thread does the close; the next sentence starts after it.
  Generation releases the GIL (measured), so the drain loop keeps polling
  for stop requests at its normal rhythm while a sentence renders.
- The output is opened when an item starts and closed when the queue runs
  dry, so sentences in one item join without a re-open, and a warm, waiting
  engine holds no device.
- Audio output is one small "sink" with two implementations, chosen once
  when the engine starts:
  - `sounddevice` (PortAudio), used wherever it loads. Its macOS and Windows
    wheels are expected to bundle PortAudio.
  - A player process fed raw audio on standard input (`aplay`, `pw-cat`,
    `paplay` or `ffplay`, whichever exists), used when `sounddevice` cannot
    load. Measured: on Linux the wheel does not bundle PortAudio and the
    import fails without the system library, so this is the normal Linux
    path, not a rare fallback. Killing the player is the silence.
  - With neither available the engine logs why and exits through the
    existing failed-open back-off; setup reports the missing player.
- `prefetch()` is a no-op: a warm first chunk arrives in under 35 ms.

### Model

- `ptts` downloads `gradium/phonon-31m-en` from Hugging Face on first use
  into the standard Hugging Face cache (`$HF_HOME/hub`, by convention
  `~/.cache/huggingface/hub`). The 8-bit weights file is 64.5MB; the tokenizer,
  config and default voice add under 1MB. Once cached, the model and any
  cached voice load with the network off (measured).
- "Phonon is installed" means the Phonon environment exists and the model's
  snapshot folder holds the 8-bit weights. That check reads the folder and
  loads nothing.
- `/voiceover:setup` offers the download ahead of time. An explicit `phonon`
  selection with no model downloads it on first narration, the same as an
  explicit `kokoro` selection does today.

### Voices and the catalogue

- Phonon's English catalogue has 37 voices: 25 standard voices and 12
  character voices (a pirate captain, a dragon, a fairy godmother and so
  on). The model also carries one bundled voice called `default`.
- Each catalogue voice is its own 256KB file, fetched from Hugging Face the
  first time it is used. An uncached voice fails with no network (measured).
  So:
  - Choosing a voice plays a sample, as today, and that fetch is what caches
    it. If the fetch fails the command says so and the setting is not saved.
  - At narration time, a voice that cannot be loaded falls back to the
    bundled `default` voice and logs it, rather than going silent.
- `scripts/manage_voices.py`'s catalogue gains all 37, each tagged
  `engine: "phonon"`, under lower-case friendly names (`bryony`, `maeve`,
  `captain-silas-blackwater`). None collides with a Kokoro or macOS friendly
  name, so Kokoro's thirteen names keep their meaning and no prefix is
  needed. List, set, test and availability become engine-generic rather than
  Kokoro-or-macOS.
- The per-project automatic pick under Phonon chooses among the 25 standard
  voices only. A project never lands on the demon by hash.
- No speaking-rate control for Phonon in this release. `ptts` has no
  per-call speed; its only control is set on the model instance and measured
  as non-monotonic. Phonon voices have no per-voice speed table.
- `/voiceover:voice` lists voices grouped by engine. Its description names
  the engines rather than every voice.

### Setup, kill switch, docs

- `/voiceover:setup` and `scripts/setup_tts.py` report Phonon's status and
  offer it **first** (about 65MB), then Kokoro (about 300MB). Nothing is
  installed without the user saying yes.
- Installing never selects. The engine changes only when the user picks a
  voice in setup's voice step or with `/voiceover:voice`. So a user who
  re-runs setup and keeps their voice keeps their engine.
- `manage_voices.py recommend` prefers a Phonon voice when Phonon is
  installed, then Kokoro when its models are present, then the macOS voice.
  It only prints a suggestion. Which Phonon voice it names is one the user
  chooses after hearing them; until then the builder uses the first built-in
  voice as a placeholder.
- `setup_tts.py --test` speaks through the resolved engine as today. When
  Phonon is installed but not selected, the report says so and names the
  command that selects it, rather than reporting no usable engine.
- `tts/tts_controller.py` and `voiceover/process_utils.py` learn the Phonon
  script name, for both the pattern kill and the lock-owner name check, on
  Unix and Windows. One-shot versus drainer patterns keep their separation.
- `manage_settings.py` accepts `phonon` as an engine and reports Phonon's
  install state.
- README, INTERFACES.md, CHANGELOG, both command files and the plugin
  manifest's description and keywords are updated. Version 1.6.0.

## Calls made by the designer (not the user's decisions)

- A drainer steps aside for another engine's pending items (the section on
  switching engines). It follows from goal 3, but it changes shared code
  that Kokoro and macOS speech also run, so it is flagged first.
- Installing an engine never selects it; `recommend` prefers Phonon when it
  is installed.
- Kokoro keeps passing any explicit voice string through unchanged.
- `sounddevice` where it loads, a piped player process otherwise.
- All 37 voices exposed; the automatic pick limited to the 25 standard ones;
  fall back to the bundled `default` voice when a voice cannot be loaded.
- 8-bit weights and 2 threads, with no setting for either.
- No speaking-rate control for Phonon.
- The standard Hugging Face cache, not a folder of the plugin's own.
- Setup offers Phonon before Kokoro. `manage_voices.py recommend` keeps
  recommending what already works on the machine, and mentions Phonon.
- First-use automatic model download, matching Kokoro's existing behaviour.
- Output stream opened per item, closed when the queue is dry.
- CPU only; no thread-count setting.
- The engine script lives in `tts/` with its uv project in `tts-phonon/`.

## Measured, and still unverified

Measured on 22 Nov 2026 by a separate worker session, with `ptts` 0.7.1 from
PyPI on an Intel i7-9750H laptop (6 cores, Ubuntu 26.04, Python 3.12 and
3.14). The figures quoted in this document as "measured" come from that
report. The machine is a 2019 laptop, not a weak one, so Gradium's claims for
low-end hardware remain the vendor's own.

One caution for the builder: peak memory after speaking was 300 to 400 MiB
with the 8-bit weights. It has not been compared with Kokoro's, so "lighter
than Kokoro" is established for disk and speed, not yet for memory.

The user has listened to Phonon's output, generated on that machine and
played through a web page, and judged it good. That settles voice quality.
It does not exercise the engine's own audio output, since a browser did the
playing.

Still unverified. The plan's first task is a throwaway spike, on a machine
with working sound, that settles each before product code is written:

1. Audio playback end to end. Nothing has been played yet: the test machine
   had no PortAudio and no local speaker. Verify on macOS that `sounddevice`
   loads from its wheel alone, that streamed chunks play without gaps, and
   that aborting silences at once. Verify the piped-player path on Linux the
   same way, including which player and raw format work under PipeWire.
2. Stop latency as heard: from a stop request to silence, with the
   silence-first order above.
3. That loading with the 8-bit weights on a clean cache downloads only the
   8-bit file, not the 103MB full-precision file as well.
4. Windows: that the engine, the `sounddevice` wheel and the kill switch
   behave. No Windows run exists.

If the spike contradicts the design (for example, audio cannot be made
gapless, or silence is not immediate), stop and bring it back to the user.

### Spike results (22 Nov 2026, plan Task 1)

Run by the builder session on an Apple-silicon Mac (macOS 27.2, Python 3.12,
`ptts` 0.7.1, `sounddevice` 0.5.6, output to an external USB speaker), with a
throwaway venv and a scratch `HOME`, outside the repo. "Measured" means the
program observed it; nobody listened, because the user was away. "Heard" is
therefore not done for every audio item.

1. `sounddevice` on macOS: **measured.** `import sounddevice` loads the
   `libportaudio.dylib` bundled in its own wheel (not Homebrew's), with no
   system package needed.
2. Voice per stream: **measured.** `tts.stream(text, voice="Bryony")`. Names
   match in any case (`"bryony"` works) and with spaces
   (`"Captain Silas Blackwater"`). A voice that cannot be used raises from the
   `stream()` call itself, before any chunk: `LookupError` for an unknown name
   (`"Missing"`, `"af_sky"`) or an uncached voice under `HF_HUB_OFFLINE=1`;
   `OSError` ("cannot fetch …") for an uncached voice when the network fails,
   after about 2.6 s. The bundled `default` voice works offline.
   `ptts.catalog_voices()` lists exactly the 37 English names in this spec.
3. Gapless streaming: **measured, not heard.** A 12 s sentence streamed into
   PortAudio produced zero output underflows, both with a blocking
   `RawOutputStream.write` and with a callback-fed stream. First chunk 16 to
   23 ms after a warm-up. The first chunk is 1,920 samples; later chunks are
   larger (14 chunks for 12 s).
4. Stop: **measured, not heard.** The plan's sink (blocking `write()` on a
   worker thread, `abort()` from another thread) **crashes the process with a
   bus error** inside `sounddevice._raw_write`, and a same-thread `abort()`
   itself blocks for 107 ms. A **callback-fed stream** works instead: the
   worker appends chunk bytes to a buffer the sink owns, the PortAudio callback
   copies from it (zeros when empty), and stop empties the buffer. With
   `latency="low"` and `blocksize=480` (20 ms): `kill()` returns in under
   0.1 ms, the first silent callback follows 10 to 15 ms later and reaches the
   device 14 ms after that (25 to 30 ms in all, from PortAudio's own
   timestamps), 50 callbacks per second, zero underflows, no crash over three
   kills, and the next sentence plays on the same open stream. Opening and
   starting the stream takes 5 to 7 ms; `stop()`/`abort()` plus `close()`
   takes 108 to 121 ms on macOS whichever way it is done.
5. Linux piped player: **not done** (no Linux machine here). Measured on macOS
   with `ffplay` 9.0.2 instead: `-ac 1` is rejected ("Option not found"); the
   raw demuxer's default is one channel, so
   `ffplay -f f32le -ar 24000 -nodisp -autoexit -loglevel error -i -` plays
   (4.6 s of audio, the player exited 4.35 s after its input closed), and
   killing it ends the process in 12 ms. `aplay`, `paplay` and `pw-cat`
   arguments are untested.
6. Model download: **measured.** On an empty cache with `HF_HOME` unset,
   `quant="q8_0"` downloads only `model.q8.gguf` (64,512,352 bytes),
   `tokenizer.json`, `config.json` and `default-voice.safetensors` (about
   0.5 MB together), into `~/.cache/huggingface/hub/models--gradium--phonon-31m-en/snapshots/<rev>/`.
   The 103 MB full-precision file is not fetched. The unpinned name
   `hf://gradium/phonon-31m-en` checks the network on every load: 1.6 s
   online, and **17 s** with the network unreachable before it falls back to
   the cache. Pinned to a revision
   (`hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f`,
   which is `ptts.DEFAULT_MODEL` in 0.7.1 and has the same files), it loads in
   0.41 s online or offline. With no `device` argument the model ran on `cpu`
   (`ptts.available_devices()` lists `metal` and `cpu` on this build).
7. Memory: **measured, as one-shot runs rather than drains.** Speaking the
   same three sentences, Phonon (a script with the callback sink) peaked at
   343 MiB resident and used 0.7 s of CPU; Kokoro (`kokoro_voice.py --stream`
   under `uv run`) peaked at 724 MiB and used 8.5 s of CPU.
8. Windows: **not done.**

Gate: a catalogue voice can be chosen per stream; streaming showed no
underflow; stop reaches silence in about 30 ms with the callback sink. The
gate passes with the callback sink, not with the sink the plan describes.

## Left to the builder

- How playback helpers are shared between the two engine scripts without
  breaking `engine_common`'s stdlib-only rule.
- The shape of the engine-generic voice catalogue and availability checks.
- Test layout: a `ptts` and `sounddevice` stub in the style of
  `test_tts_device.py`; tests for engine selection, engine-aware voice
  resolution, the drain with a Phonon speaker, stop mid-sentence, the kill
  switch patterns, and a manifest test that the two environments share no
  engine dependency.

## Sources

Vendor claims, not independently benchmarked.

- https://github.com/gradium-ai/xn-ptts
- https://pypi.org/project/ptts/
- https://gradium.ai/blog/release-phonon-open-weight
- https://huggingface.co/gradium/phonon-31m-en
