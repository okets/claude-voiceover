"""enqueue_speech gates like speak() but never loses to the lock."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report

data_dir = isolate()
from voiceover import spool, speech
from voiceover.settings import set_setting

set_setting("interaction_level", "narrator")
set_setting("tts_engine", "kokoro")
set_setting("voice", "bf_emma")


def texts():
    out = []
    for path in sorted(spool.queue_dir().glob("*.json")):
        out.append(json.loads(path.read_text())["text"])
    return out


# --- speak() behavior preservation tests (behavior-preserving refactor guard) ---
os.environ["VOICEOVER_DRY_RUN"] = "1"
check("speak returns True at narrator level", speech.speak("hello") is True)
set_setting("interaction_level", "silent")
check("speak returns False when level is silent", speech.speak("hello") is False)
set_setting("interaction_level", "narrator")
check("speak returns False for empty text", speech.speak("   ") is False)
long_text = " ".join(["word"] * 400)
check("speak with full=False truncates", speech.speak(long_text, full=False) is True)
check("speak with full=True does not truncate", speech.speak(long_text, full=True) is True)
del os.environ["VOICEOVER_DRY_RUN"]

# --- the basic path ---------------------------------------------------------
check("enqueue_speech returns True", speech.enqueue_speech("hello there", full=True) is True)
check("the text landed in the spool", texts() == ["hello there"], texts())

# --- it resolves engine and voice ------------------------------------------
item = json.loads(sorted(spool.queue_dir().glob("*.json"))[0].read_text())
check("engine is resolved at enqueue time", item["engine"] == "kokoro", item)
check("voice is resolved at enqueue time", item["voice"] == "bf_emma", item)

# --- THE POINT: a held lock does not stop queueing -------------------------
spool.clear()
speech.lock_path().write_text(json.dumps({"expiry": time.time() + 60, "pid": 1}))
check("the lock reads as held", speech._is_locked() is True)
check("enqueue_speech still succeeds while audio plays",
      speech.enqueue_speech("queued during playback", full=True) is True)
check("the text is queued, not dropped", texts() == ["queued during playback"], texts())
speech.lock_path().unlink()

# --- level gating still applies --------------------------------------------
spool.clear()
set_setting("interaction_level", "silent")
check("silent level enqueues nothing", speech.enqueue_speech("shh", full=True) is False)
check("the spool stayed empty", spool.pending_count() == 0)
set_setting("interaction_level", "narrator")

# --- truncation contract mirrors speak() -----------------------------------
spool.clear()
long_text = " ".join(["word"] * 400)
speech.enqueue_speech(long_text, full=False)
short = texts()[0]
check("full=False truncates like speak()", len(short) < len(long_text), len(short))
spool.clear()
speech.enqueue_speech(long_text, full=True)
check("full=True keeps the whole text", len(texts()[0]) > 1000, len(texts()[0]))

# --- empty text is refused --------------------------------------------------
spool.clear()
check("empty text is refused", speech.enqueue_speech("   ", full=True) is False)
check("whitespace never reaches the spool", spool.pending_count() == 0)

# --- session tagging --------------------------------------------------------
spool.clear()
speech.enqueue_speech("tagged", full=True, session="S1")
item = json.loads(sorted(spool.queue_dir().glob("*.json"))[0].read_text())
check("the session id is recorded", item["session"] == "S1", item)

# --- ensure_drainer -------------------------------------------------------
spool.clear()
check("no drainer is spawned for an empty spool", speech.ensure_drainer() is False)
speech.enqueue_speech("something to say", full=True)
speech.lock_path().write_text(json.dumps({"expiry": time.time() + 60, "pid": 1}))
check("no drainer is spawned while one already holds the lock",
      speech.ensure_drainer() is False)
speech.lock_path().unlink()
check("a drainer is spawned for a non-empty spool with a free lock",
      speech.ensure_drainer() is True)

report()
