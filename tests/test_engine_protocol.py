# tests/test_engine_protocol.py
"""Engine protocol 1 primitives: line framing, PCM encoding, audio frames."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from engine_protocol import (BadLine, ERROR_CODES, LineReader, audio_header,
                             encode_line, pcm16le, split_frames)

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
r4 = LineReader(); partial = r4.feed(b"x" * 70000); rest = r4.feed(b"yy\n" + b'{"op":"status"}\n')
check("over-long line across chunks: one BadLine, rest discarded",
      len(partial) == 1 and isinstance(partial[0], BadLine) and rest == [{"op": "status"}])
check("non-object JSON -> bad_request", isinstance(LineReader().feed(b"[1]\n")[0], BadLine))
check("invalid JSON -> bad_request", LineReader().feed(b"{nope\n")[0].code == "bad_request")
check("deeply nested JSON -> bad_request, not a crash",
      LineReader().feed(b"[" * 60000 + b"\n")[0].code == "bad_request")
check("blank lines are ignored", LineReader().feed(b"\n\n") == [])
check("encode_line is compact, newline-terminated", encode_line({"a": 1, "b": "é"}) == '{"a":1,"b":"é"}\n'.encode())
check("pcm clip and little-endian", pcm16le([0.0, 1.0, -1.0, 2.0]) == b"\x00\x00\xff\x7f\x01\x80\xff\x7f")
frames = split_frames(b"\x01\x00" * 40000)
check("frames even and capped", all(len(f) % 2 == 0 and len(f) <= 32768 for f in frames) and sum(map(len, frames)) == 80000)
check("odd size is rounded down to even", all(len(f) % 2 == 0 for f in split_frames(b"\x00" * 10, 5)))
check("audio header", json.loads(audio_header("u1", 0, 32768)) == {"ev": "audio", "id": "u1", "seq": 0, "bytes": 32768})
check("unauthorized is an error code", "unauthorized" in ERROR_CODES)

report()
