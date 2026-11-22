"""tts/pcm_sink.py: play raw 24 kHz mono float32 audio as it arrives.

No real audio: sounddevice is a stub whose stream records calls and whose
callback the test drives by hand, and the piped player is a small Python
process. Touches no project settings, so it runs the same from any cwd.
"""

import importlib.abc
import importlib.machinery
import os
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, temp_dir, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))

BLOCK_BYTES = 480 * 4  # one 20 ms callback block of float32 samples


class Chunk:
    """Stands in for a numpy float32 chunk: len() in samples, tobytes()."""

    def __init__(self, samples, fill=b"\x01"):
        self._samples, self._fill = samples, fill

    def __len__(self):
        return self._samples

    def tobytes(self):
        return self._fill * (self._samples * 4)


# --- a stub sounddevice ---------------------------------------------------------------
calls = []
streams = []


class StubStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs.get("callback")
        streams.append(self)
        calls.append("open")

    def start(self):
        calls.append("start")

    def stop(self):
        calls.append("stop")

    def abort(self):
        calls.append("abort")

    def close(self):
        calls.append("close")

    def write(self, data):
        calls.append("write")  # the spike's crash: no code path may ever call this


def install_stub_sounddevice(check_fails=False):
    module = types.ModuleType("sounddevice")
    module.RawOutputStream = StubStream

    class PortAudioError(Exception):
        pass

    def check_output_settings(**kwargs):
        if check_fails:
            raise PortAudioError("Error querying device -1")

    module.PortAudioError = PortAudioError
    module.check_output_settings = check_output_settings
    sys.modules["sounddevice"] = module


class _Raises(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Makes `import sounddevice` raise the given error, as a missing PortAudio does."""

    def __init__(self, error):
        self.error = error

    def find_spec(self, name, path=None, target=None):
        if name == "sounddevice":
            return importlib.machinery.ModuleSpec(name, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise self.error


def sounddevice_import_raises(error):
    sys.modules.pop("sounddevice", None)
    finder = _Raises(error)
    sys.meta_path.insert(0, finder)
    return finder


def play_block(stream):
    """Run one callback block; returns the bytes it produced."""
    out = bytearray(b"\xff" * BLOCK_BYTES)
    stream.callback(out, 480, None, None)
    return bytes(out)


install_stub_sounddevice()
import pcm_sink  # noqa: E402

# --- SoundDeviceSink ------------------------------------------------------------------
sink = pcm_sink.SoundDeviceSink()
opened_before = calls.count("open")
sink.write(Chunk(1920))
check("the device opens on the first write, not before", (opened_before, calls.count("open")) == (0, 1))
stream = streams[-1]
check("the stream is callback-fed, low latency, 20 ms blocks, 24 kHz mono float32",
      stream.callback is not None and {k: stream.kwargs.get(k) for k in
                                       ("samplerate", "channels", "dtype", "latency", "blocksize")}
      == {"samplerate": 24000, "channels": 1, "dtype": "float32", "latency": "low", "blocksize": 480})
check("writing started the stream", "start" in calls)
check("remaining counts written audio", 0.07 < sink.remaining() <= 0.08, sink.remaining())
played = b"".join(play_block(stream) for _ in range(2))
check("the callback plays the written bytes", played == b"\x01" * (2 * BLOCK_BYTES))
check("remaining falls as the callback plays", abs(sink.remaining() - 0.04) < 1e-9, sink.remaining())
played = b"".join(play_block(stream) for _ in range(2))
check("the whole chunk plays out", played == b"\x01" * (2 * BLOCK_BYTES) and sink.remaining() == 0.0)
check("an empty buffer plays silence", play_block(stream) == b"\x00" * BLOCK_BYTES)

sink.write(Chunk(25, fill=b"\x02"))  # 100 bytes: less than one block
check("a partial final block plays at once, padded with zeros",
      play_block(stream) == b"\x02" * 100 + b"\x00" * (BLOCK_BYTES - 100) and sink.remaining() == 0.0)

sink.write(Chunk(1920))
calls_before_abort = list(calls)
sink.abort()
check("abort never calls into PortAudio", calls == calls_before_abort, calls[len(calls_before_abort):])
check("abort silences and resets remaining",
      play_block(stream) == b"\x00" * BLOCK_BYTES and sink.remaining() == 0.0)
sink.write(Chunk(480, fill=b"\x03"))
check("writing after abort plays on the same open stream",
      calls.count("open") == 1 and play_block(stream) == b"\x03" * BLOCK_BYTES)

sink.close()
check("close stops the stream rather than aborting it",
      calls[-2:] == ["stop", "close"] and "abort" not in calls, calls)
sink.close()
check("a second close does not re-close or re-open",
      (calls.count("close"), calls.count("stop"), calls.count("open")) == (1, 1, 1), calls)
sink.write(Chunk(480))
check("close releases the device and a later write re-opens it", calls.count("open") == 2)
sink.close()
check("no code path ever calls the stream's blocking write()", "write" not in calls)

# --- PipeSink -------------------------------------------------------------------------
work = temp_dir("vo-pcm-pipe-")


def python_player(body):
    return [sys.executable, "-c", body]


count_file = work / "count"
sink = pcm_sink.PipeSink(python_player(
    "import sys; n = len(sys.stdin.buffer.read()); open(%r, 'w').write(str(n))" % str(count_file)))
sink.write(Chunk(1920))
check("the pipe sink's remaining counts written audio by wall clock", 0.07 < sink.remaining() <= 0.08)
sink.close()
deadline = time.time() + 5
while not count_file.exists() and time.time() < deadline:
    time.sleep(0.02)
check("the pipe sink feeds the player's stdin", count_file.exists() and count_file.read_text() == "7680")

pid_file = work / "pid"
sink = pcm_sink.PipeSink(python_player(
    "import os, sys; open(%r, 'w').write(str(os.getpid())); sys.stdin.buffer.read()" % str(pid_file)))
sink.write(Chunk(480))
deadline = time.time() + 5
while not pid_file.exists() and time.time() < deadline:
    time.sleep(0.02)
player_pid = int(pid_file.read_text())
sink.abort()


def pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


check("aborting the pipe sink kills the player", not pid_running(player_pid))
check("abort resets the pipe sink's remaining", sink.remaining() == 0.0)

sink = pcm_sink.PipeSink(python_player("import sys, time; sys.stdin.buffer.read(); time.sleep(2)"))
sink.write(Chunk(480))
began = time.time()
sink.close()
close_seconds = time.time() - began
check("closing the pipe sink returns at once", close_seconds < 0.1, close_seconds)
sink.close()
check("closing it twice is harmless", True)

starts_file = work / "starts"
sink = pcm_sink.PipeSink(python_player("open(%r, 'a').write('x')" % str(starts_file)))
raised = None
try:
    sink.write(Chunk(480))
    time.sleep(0.5)                  # the player has exited on its own
    sink.write(Chunk(480))
    time.sleep(0.5)
    sink.write(Chunk(48000))         # more than a pipe holds, into a player that exits at once
except Exception as error:  # noqa: BLE001 - the check reports it
    raised = error
check("a dead player never raises into the writer", raised is None, repr(raised))
time.sleep(0.3)
check("the next write after a player died starts a new one",
      starts_file.exists() and len(starts_file.read_text()) >= 2,
      starts_file.read_text() if starts_file.exists() else "no starts")
sink.abort()

# --- player_command -------------------------------------------------------------------
real_which = pcm_sink.shutil.which
try:
    pcm_sink.shutil.which = lambda name: "/usr/bin/" + name
    check("aplay comes first when every player exists",
          pcm_sink.player_command() == ["aplay", "-q", "-t", "raw", "-f", "FLOAT_LE", "-r", "24000", "-c", "1", "-"])
    pcm_sink.shutil.which = lambda name: "/opt/bin/ffplay" if name == "ffplay" else None
    check("ffplay uses the measured arguments (no -ac)",
          pcm_sink.player_command() == ["ffplay", "-f", "f32le", "-ar", "24000", "-nodisp", "-autoexit",
                                        "-loglevel", "error", "-i", "-"])
    pcm_sink.shutil.which = lambda name: "/usr/bin/pw-cat" if name == "pw-cat" else None
    check("pw-cat when it is the only player",
          (pcm_sink.player_command() or [None])[0] == "pw-cat")
    pcm_sink.shutil.which = lambda name: None
    check("no player gives None", pcm_sink.player_command() is None)

    # --- open_sink never raises -------------------------------------------------------
    pcm_sink.shutil.which = lambda name: "/opt/bin/ffplay" if name == "ffplay" else None
    install_stub_sounddevice()
    check("sounddevice that loads is preferred", isinstance(pcm_sink.open_sink(), pcm_sink.SoundDeviceSink))
    install_stub_sounddevice(check_fails=True)
    check("no output device falls back to the piped player", isinstance(pcm_sink.open_sink(), pcm_sink.PipeSink))
    for error in (OSError("PortAudio library not found"), ImportError("No module named 'sounddevice'")):
        finder = sounddevice_import_raises(error)
        try:
            result = pcm_sink.open_sink()
        finally:
            sys.meta_path.remove(finder)
        check("%s on import falls back to the piped player" % type(error).__name__,
              isinstance(result, pcm_sink.PipeSink))
    finder = sounddevice_import_raises(OSError("PortAudio library not found"))
    pcm_sink.shutil.which = lambda name: None
    try:
        check("no device and no player gives None", pcm_sink.open_sink() is None)
        pcm_sink.shutil.which = lambda name: (_ for _ in ()).throw(RuntimeError("PATH unreadable"))
        check("open_sink never raises", pcm_sink.open_sink() is None)
    finally:
        sys.meta_path.remove(finder)
finally:
    pcm_sink.shutil.which = real_which
    install_stub_sounddevice()

report()
