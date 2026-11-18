# tts/engine_protocol.py
"""Wire primitives of the shared Kokoro engine's protocol 1. Stdlib only.

Control traffic is one JSON object per line; audio is a JSON header line
followed by that many raw PCM bytes.
"""

import json
import sys
from array import array
from typing import NamedTuple

PROTOCOL = 1
AUDIO = {"format": "pcm_s16le", "rate": 24000, "channels": 1}
LIMITS = {"line": 65536, "text": 6000, "frame": 32768}
# The protocol's error codes, as documented in INTERFACES.md ("Errors"): the only codes the
# engine sends, kept here as the reference list for client authors and tests.
ERROR_CODES = frozenset({
    "bad_request", "unsupported_protocol", "unknown_voice", "bad_speed",
    "text_too_long", "busy", "no_model", "synthesis_failed", "released",
    "restarting", "unauthorized",
})


def encode_line(obj: dict) -> bytes:
    """One compact UTF-8 JSON line."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


class BadLine(NamedTuple):
    code: str
    message: str


def _decode_line(raw: bytes):
    """A complete line -> dict, None for a blank line, or BadLine."""
    if len(raw) > LIMITS["line"]:
        return BadLine("bad_request", "line too long")
    if not raw.strip():
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        return BadLine("bad_request", "line is not valid JSON")
    if not isinstance(obj, dict):
        return BadLine("bad_request", "line is not a JSON object")
    return obj


class LineReader:
    """Splits a byte stream into JSON-object lines; decodes only whole lines."""

    def __init__(self):
        self._buffer = b""
        self._discarding = False

    def feed(self, data: bytes) -> list:
        out = []
        while data:
            newline = data.find(b"\n")
            if newline < 0:
                self._hold(data, out)
                break
            piece, data = data[:newline], data[newline + 1:]
            self._finish_line(piece, out)
        return out

    def _hold(self, piece: bytes, out: list) -> None:
        """Buffer an unterminated tail; an over-long one is reported once and dropped."""
        if self._discarding:
            return
        self._buffer += piece
        if len(self._buffer) > LIMITS["line"]:
            out.append(BadLine("bad_request", "line too long"))
            self._buffer = b""
            self._discarding = True

    def _finish_line(self, piece: bytes, out: list) -> None:
        if self._discarding:
            self._discarding = False
            return
        decoded = _decode_line(self._buffer + piece)
        self._buffer = b""
        if decoded is not None:
            out.append(decoded)


def pcm16le(samples) -> bytes:
    """Floats in [-1, 1] -> clipped little-endian int16 bytes."""
    if hasattr(samples, "astype"):
        scaled = samples.clip(-1.0, 1.0) * 32767.0  # numpy array: no import needed
        return scaled.astype("<i2").tobytes()
    packed = array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    if sys.byteorder == "big":
        packed.byteswap()
    return packed.tobytes()


def split_frames(pcm: bytes, size: int = LIMITS["frame"]) -> list:
    """PCM -> pieces that are even-length and at most `size` bytes."""
    step = max(2, size - size % 2)
    return [pcm[i:i + step] for i in range(0, len(pcm), step)]


def audio_header(req_id: str, seq: int, nbytes: int) -> bytes:
    return encode_line({"ev": "audio", "id": req_id, "seq": seq, "bytes": nbytes})
