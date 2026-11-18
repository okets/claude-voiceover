"""Narration as an engine client: EngineSpeaker through kokoro_voice's drain path,
against a real stub engine (VOICEOVER_ENGINE_STUB=1) started by ensure()."""

import json
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

DATA = isolate()
os.environ["VOICEOVER_ENGINE_STUB"] = "1"
os.environ.pop("VOICEOVER_DRY_RUN", None)
sys.path.insert(0, str(REPO_ROOT / "tts"))
from voiceover import spool, speech
import engine_client
import engine_common
import engine_speaker
import kokoro_voice

ROOT = spool.queue_dir().parent
VOICE = "af_sky"


class Clip:
    """A recorded play_file result: finishes after `lasts` seconds, or never if None."""
    def __init__(self, path, lasts):
        self.path, self.lasts, self.killed = path, lasts, False
        self.began = time.monotonic()

    def poll(self):
        return self.lasts is not None and time.monotonic() - self.began >= self.lasts

    def kill(self):
        self.killed = True


played, lasts = [], [0.0]


def fake_play_file(path, duration):
    data = Path(path).read_bytes()  # the real player would own the file; keep a copy
    played.append((Path(path), duration, data))
    return Clip(path, lasts[0])


def run_main(*argv):
    saved = sys.argv
    sys.argv = ["kokoro_voice.py"] + list(argv)
    try:
        return kokoro_voice.main()
    finally:
        sys.argv = saved


def wav_ok(blob, tmp=[0]):
    tmp[0] += 1
    path = DATA / ("check-%d.wav" % tmp[0])
    path.write_bytes(blob)
    with wave.open(str(path)) as w:
        return (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2) and w.getnframes() > 0


kokoro_voice.ensure_models = lambda: True
kokoro_voice.DRY_RUN = False
kokoro_voice.play_file = fake_play_file


def engine_status():
    conn = engine_client.EngineConnection.connect()
    try:
        return conn.status()
    finally:
        conn.close()


def stop_engine():
    try:
        conn = engine_client.EngineConnection.connect()
        conn.stop()
        conn.close()
    except Exception:
        pass


try:
    # --- commands ------------------------------------------------------------
    cmd = speech.kokoro_command("--drain", "/x")
    check("kokoro_command runs plain python on Linux", cmd[0] == sys.executable
          and cmd[1].endswith("kokoro_voice.py") and cmd[2:] == ["--drain", "/x"], cmd)

    # --- the plain-python import chain -----------------------------------------
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import kokoro_voice, engine_speaker; "
         "bad = [m for m in ('numpy', 'soundfile', 'kokoro_onnx', 'onnxruntime') if m in sys.modules]; "
         "sys.exit(1 if bad else 0)" % str(REPO_ROOT / "tts")],
        env=dict(os.environ), capture_output=True, text=True)
    check("kokoro_voice imports no numpy/soundfile/kokoro", probe.returncode == 0, probe.stderr)

    # --- drain: three sentences, three wavs ------------------------------------
    spool.clear()
    spool.enqueue("One. Two. Three.", "kokoro", VOICE, utterance="U")
    check("drain exits 0", run_main("--drain", str(ROOT)) == 0)
    check("three sentences played as three wavs", len(played) == 3, played)
    check("each is a valid 24 kHz mono 16-bit wav", all(wav_ok(blob) for _, _, blob in played))
    check("named <pid>-<n>.wav in play/", all(p.parent == ROOT / "play"
          and p.name == "%d-%d.wav" % (os.getpid(), i + 1) for i, (p, _, _) in enumerate(played)),
          [p for p, _, _ in played])

    # --- prefetch: the 2nd request goes out before the 1st finishes playing ------
    submitted = []
    conn = engine_client.EngineConnection.connect()
    real_submit = conn.submit
    conn.submit = lambda *a, **k: (submitted.append((time.monotonic(), a)), real_submit(*a, **k))[1]
    speaker = engine_speaker.EngineSpeaker(lambda: conn, DATA / "play2", fake_play_file)
    lasts[0] = 0.6
    playback = speaker.begin("One.", VOICE, "Two.")
    deadline = time.monotonic() + 5
    while not playback.started() and time.monotonic() < deadline:
        playback.poll()
        time.sleep(0.01)
    check("the first sentence started playing", playback.started())
    check("the 2nd sentence is already requested, with no speed",
          len(submitted) == 2 and submitted[1][1] == ("Two.", VOICE), submitted)
    check("and the first request was submitted with no speed", submitted[0][1] == ("One.", VOICE))
    next_playback = speaker.begin("Two.", VOICE)
    check("begin reuses the prefetched request", len(submitted) == 2, submitted)
    other = speaker.begin("Three.", VOICE)  # not prefetched: new request
    check("a non-prefetched begin submits", len(submitted) == 3)
    check("EnginePlayback.started false before play", not next_playback.started())
    playback.kill()
    other.kill()
    next_playback.kill()
    conn.close()
    lasts[0] = 0.0

    # --- stop request mid-item -------------------------------------------------
    del played[:]
    lasts[0] = None  # playback never ends by itself
    clips = []
    kokoro_voice.play_file = lambda path, duration: clips.append(fake_play_file(path, duration)) or clips[-1]
    spool.clear()
    item = spool.enqueue("Alpha one. Beta two. Gamma three.", "kokoro", VOICE, utterance="S", session="S1")
    codes = []
    thread = threading.Thread(target=lambda: codes.append(run_main("--drain", str(ROOT))))
    thread.start()
    deadline = time.monotonic() + 8
    while not clips and time.monotonic() < deadline:
        time.sleep(0.02)
    check("playback began", bool(clips))
    time.sleep(0.2)
    speech.request_stop(session="S1")
    thread.join(10)
    check("the drain ended after the stop", not thread.is_alive() and codes == [0], codes)
    check("the stop killed playback", clips and clips[0].killed)
    deadline = time.monotonic() + 0.5
    requests = engine_status()["requests"]
    while requests and time.monotonic() < deadline:
        time.sleep(0.02)
        requests = engine_status()["requests"]
    check("the engine has no request left active", requests == 0, requests)
    check("no later sentence played", len(clips) == 1, len(clips))
    kokoro_voice.play_file = fake_play_file
    lasts[0] = 0.0

    # --- warm while waiting -----------------------------------------------------
    now = [0.0]
    conn = engine_client.EngineConnection.connect()
    warms = []
    real_warm = conn.warm
    conn.warm = lambda hold=0: (warms.append(hold), real_warm(hold))[1]
    speaker = engine_speaker.EngineSpeaker(lambda: conn, DATA / "play3", fake_play_file, clock=lambda: now[0])
    speaker.warm_up()
    check("warm_up warms for 60 s", warms == [60], warms)
    deadline = time.monotonic() + 2
    while engine_status()["leases"] != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    check("the engine shows the lease", engine_status()["leases"] >= 1, engine_status())
    now[0] = 10
    speaker.idle()
    check("idle within 30 s sends nothing", warms == [60], warms)
    now[0] = 31
    speaker.idle()
    now[0] = 40
    speaker.idle()
    check("idle warms again after 30 s, at most every 30 s", warms == [60, 60], warms)
    conn.close()

    # drain wires idle() in while a lease is live and the queue is empty
    class IdleSpeaker:
        idles = 0
        def idle(self):
            IdleSpeaker.idles += 1
    spool.clear()
    lease_dir = ROOT / "leases"
    lease_dir.mkdir(parents=True, exist_ok=True)
    lease_file = lease_dir / "S9.json"
    lease_file.write_text(json.dumps({"expiry": time.time() + 0.4}))
    engine_common.drain(ROOT, IdleSpeaker, {"kokoro"}, poll_seconds=0.05)
    check("drain calls idle() while it waits on a live lease", IdleSpeaker.idles >= 2, IdleSpeaker.idles)
    lease_file.unlink()

    # --- reconnect once on restarting / a dead connection -----------------------
    conns = []

    def connect():
        conns.append(engine_client.EngineConnection.connect())
        return conns[-1]

    speaker = engine_speaker.EngineSpeaker(connect, DATA / "play4", fake_play_file)
    speaker.warm_up()
    conns[0].close()
    time.sleep(0.2)
    playback = speaker.begin("Hello there.", VOICE)
    deadline = time.monotonic() + 5
    while not playback.started() and time.monotonic() < deadline:
        playback.poll()
        time.sleep(0.01)
    check("a dead connection at submit reconnects once", len(conns) == 2 and playback.started(), len(conns))

    # a request that ends restarting is resubmitted once; a second failure raises
    playback = speaker.begin("Again.", VOICE)
    pending = playback._pending
    pending._finish(reason="error", error=engine_client.EngineError("restarting"))
    deadline = time.monotonic() + 5
    while not playback.started() and time.monotonic() < deadline:
        playback.poll()
        time.sleep(0.01)
    check("restarting is retried through connect() once", len(conns) == 3 and playback.started(), len(conns))
    playback = speaker.begin("Boom.", VOICE)
    playback._pending._finish(reason="error", error=engine_client.EngineError("restarting"))
    playback.poll()  # the one retry
    playback._pending._finish(reason="error", error=engine_client.EngineError("restarting"))
    raised = None
    try:
        playback.poll()
    except engine_client.EngineError as error:
        raised = error.code
    check("a second restarting raises EngineError from poll", raised == "restarting", raised)
    connects = len(conns)
    fatal = speaker.begin("Nope.", "no_such_voice")
    raised = None
    deadline = time.monotonic() + 5
    while raised is None and time.monotonic() < deadline:
        try:
            fatal.poll()
        except engine_client.EngineError as error:
            raised = error.code
        time.sleep(0.01)
    check("an engine error raises without retry", raised == "unknown_voice" and len(conns) == connects, (raised, len(conns)))
    for c in conns:
        c.close()

    # --- one-shot speak_text ----------------------------------------------------
    del played[:]
    updates = []
    kokoro_voice.play_audio_file = lambda path, timeout=30: (played.append(Path(path).read_bytes()), True)[1]
    check("speak_text plays the whole result as one wav",
          kokoro_voice.speak_text("Hello there friend.", VOICE, False) is True
          and len(played) == 1 and wav_ok(played[0]), len(played))
    del played[:]
    check("speak_text --stream plays frame groups as wavs",
          kokoro_voice.speak_text("Hello there friend.", VOICE, True) is True
          and len(played) >= 1 and all(wav_ok(b) for b in played), len(played))
    check("speak_text releases the tts lock", not engine_common.lock_is_live(engine_common.data_dir()))
    check("speak_text with an unknown voice fails quietly",
          kokoro_voice.speak_text("Hello.", "no_such_voice", False) is False)

    # --- one-shot downloads a missing model first, like 1.5.0's preview ------------------
    ensure_calls = []
    kokoro_voice.ensure_models = lambda: (ensure_calls.append(1), True)[1]
    kokoro_voice.speak_text("Hello there friend.", VOICE, False)
    check("speak_text ensures the models before using the engine", ensure_calls == [1], ensure_calls)
    kokoro_voice.ensure_models = lambda: False
    del played[:]
    check("speak_text returns False quietly when the model download fails",
          kokoro_voice.speak_text("Hello.", VOICE, False) is False and not played)
    kokoro_voice.ensure_models = lambda: True

    # --- an empty voice means the default voice, as in KokoroSpeaker ----------------------
    asked = []
    conn = engine_client.EngineConnection.connect()
    real_submit = conn.submit
    conn.submit = lambda text, voice, *a, **k: (asked.append(voice), real_submit(text, voice, *a, **k))[1]
    speaker = engine_speaker.EngineSpeaker(lambda: conn, DATA / "play4", fake_play_file)
    speaker.prefetch("One.", None)
    speaker.begin("Two.", "")
    speaker.cancel_all([])
    check("prefetch and begin use DEFAULT_VOICE for an empty voice",
          asked == [kokoro_voice.DEFAULT_VOICE] * 2, asked)
    conn.close()

    # --- engine down -----------------------------------------------------------------
    kokoro_voice.ensure = lambda root, timeout=10.0: False
    spool.clear()
    spool.enqueue("One. Two.", "kokoro", VOICE, utterance="D")
    before = time.time()
    code = run_main("--drain", str(ROOT))
    spawned = json.loads((ROOT / "spawned.json").read_text())
    check("engine down: drain exits 0", code == 0, code)
    check("engine down: spawned.json is pushed ~27 s ahead", spawned["ts"] > before + 25, spawned)
    check("engine down: nothing played", True)
finally:
    stop_engine()

report()
