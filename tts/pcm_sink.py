# tts/pcm_sink.py
"""Play raw 24 kHz mono float32 audio as it arrives, for the streaming engines.

Stdlib only at import: sounddevice is imported inside SoundDeviceSink, so this
module loads anywhere, and the engine falls back to a piped player process
where PortAudio cannot load (the normal case on Linux, measured).

A sink takes chunks with len() in samples and tobytes() (numpy float32
arrays from ptts); nothing here imports numpy.

Every sink offers:
    write(chunk)   play it after what is already written; opens the output on demand
    abort()        silence now, discarding what is buffered; the next write plays again
    close()        release the output once written audio has played; idempotent;
                   the next write re-opens
    remaining()    seconds of written audio not yet played
"""

import shutil
import subprocess
import threading
import time

SAMPLE_RATE = 24000
_BYTES_PER_SAMPLE = 4  # float32
_BLOCK_FRAMES = 480    # 20 ms per callback (measured: 50 callbacks/s, silence ~30 ms after abort)

# Raw float32 mono at 24 kHz on standard input, first available wins. The
# ffplay line was measured (ffplay 9.0.2 rejects "-ac 1"; raw input defaults
# to mono). The aplay, paplay and pw-cat lines are UNTESTED: no Linux machine
# was available for the spike.
_PLAYERS = (
    ["aplay", "-q", "-t", "raw", "-f", "FLOAT_LE", "-r", "24000", "-c", "1", "-"],
    ["paplay", "--raw", "--format=float32le", "--rate=24000", "--channels=1"],
    ["pw-cat", "--playback", "--raw", "--format", "f32", "--rate", "24000", "--channels", "1", "-"],
    ["ffplay", "-f", "f32le", "-ar", "24000", "-nodisp", "-autoexit", "-loglevel", "error", "-i", "-"],
)
PLAYER_NAMES = tuple(command[0] for command in _PLAYERS)  # setup reports which is on PATH


class Sink:
    """The interface both sinks implement."""

    def write(self, chunk) -> None:
        raise NotImplementedError

    def abort(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def remaining(self) -> float:
        raise NotImplementedError


class SoundDeviceSink(Sink):
    """A PortAudio output fed by its own callback from a buffer this sink owns.

    The spike measured that a blocking stream write() on one thread with
    abort() from another crashes the process (bus error). So nothing here
    calls write(): the worker appends to the buffer, PortAudio's callback
    copies from it, and abort() only empties it - it never calls into
    PortAudio. One lock guards the buffer and is held only for a slice; the
    callback does nothing slow. A second lock serialises opening and closing
    the stream, and the callback never takes it.
    """

    def __init__(self):
        import sounddevice  # raises where PortAudio cannot load

        sounddevice.check_output_settings(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        self._sounddevice = sounddevice
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._stream = None
        self._stream_lock = threading.Lock()
        self._silence = memoryview(bytes(_BLOCK_FRAMES * _BYTES_PER_SAMPLE))

    def write(self, chunk) -> None:
        data = chunk.tobytes()
        with self._buffer_lock:
            self._buffer.extend(data)
        try:
            self._ensure_open()
        except Exception:
            # The device is gone or changed: what is buffered belongs to a
            # sentence that is about to be reported failed. A later open
            # must not play it.
            self.abort()
            raise

    def abort(self) -> None:
        with self._buffer_lock:
            self._buffer.clear()

    def close(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
            if stream is None:
                return
            try:
                stream.stop()  # not abort(): a tail still in the device plays out
                stream.close()
            except Exception:
                pass

    def remaining(self) -> float:
        """Seconds still in OUR buffer. The device's own output latency (about
        25 ms) is not added: it would never reach zero once the buffer is empty."""
        with self._buffer_lock:
            return len(self._buffer) / float(_BYTES_PER_SAMPLE * SAMPLE_RATE)

    def _ensure_open(self):
        with self._stream_lock:
            if self._stream is not None:
                return
            stream = self._sounddevice.RawOutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", latency="low",
                blocksize=_BLOCK_FRAMES, callback=self._fill)
            stream.start()
            self._stream = stream

    def _fill(self, outdata, frames, time_info, status):
        """PortAudio's callback: the next bytes of the buffer, zeros after them."""
        wanted = len(outdata)
        with self._buffer_lock:
            taken = self._buffer[:wanted]
            del self._buffer[:wanted]
        outdata[:len(taken)] = taken
        if len(taken) < wanted:
            outdata[len(taken):] = self._silence[:wanted - len(taken)]


class PipeSink(Sink):
    """A player process fed raw audio on standard input. Killing it is the silence.

    A player that has died (killed, or exited on its own) never raises into
    the writer: the chunk is dropped and the next write starts a new player.
    """

    def __init__(self, command):
        self._command = list(command)
        self._process = None
        self._lock = threading.Lock()
        self._ends = 0.0

    def write(self, chunk) -> None:
        data = chunk.tobytes()
        process = self._player()
        self._extend_clock(len(chunk))
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (OSError, ValueError):  # BrokenPipeError, or stdin closed by abort()
            self._forget(process)

    def abort(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._ends = 0.0
        if process is not None:
            _kill(process)

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            try:
                process.stdin.close()  # the player plays what it has, then exits
            except (OSError, ValueError):
                pass

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._ends - time.time())

    def _player(self):
        """The running player, starting a new one when there is none or it died."""
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._process = subprocess.Popen(
                    self._command, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._process

    def _extend_clock(self, samples):
        with self._lock:
            self._ends = max(time.time(), self._ends) + samples / float(SAMPLE_RATE)

    def _forget(self, process):
        with self._lock:
            if self._process is process:
                self._process = None
        _kill(process)


def _kill(process):
    try:
        process.kill()
        process.wait(timeout=1)
    except Exception:
        pass


def player_command():
    """The first available player's argv, or None when there is none."""
    for command in _PLAYERS:
        if shutil.which(command[0]):
            return list(command)
    return None


def open_sink():
    """SoundDeviceSink where PortAudio loads and an output exists, else a
    PipeSink when a player is on PATH, else None. Never raises."""
    try:
        return SoundDeviceSink()
    except Exception:
        pass
    try:
        command = player_command()
    except Exception:
        return None
    return PipeSink(command) if command else None
