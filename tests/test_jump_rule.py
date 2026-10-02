"""The engine loop: sentences, the jump rule, sticky items, stops, prefetch."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
from voiceover import spool, speech

sys.path.insert(0, str(REPO_ROOT / "tts"))
import engine_common

ROOT = spool.queue_dir().parent
OWN = {"kokoro"}


class Recorder:
    """Records sentences and prefetches; each sentence plays for `polls`
    polls. on_begin(sentence) may act mid-drain: enqueue, request a stop."""

    def __init__(self, polls=0, on_begin=None):
        self.spoken, self.killed, self.prefetched = [], [], []
        self._polls, self._on_begin = polls, on_begin

    def prefetch(self, sentence, voice):
        self.prefetched.append(sentence)

    def begin(self, sentence, voice, upcoming=None):
        self.spoken.append(sentence)
        if self._on_begin:
            self._on_begin(sentence)
        recorder, remaining = self, [self._polls]

        class Play(engine_common.Playback):
            def poll(self):
                remaining[0] -= 1
                return remaining[0] < 0

            def kill(self):
                recorder.killed.append(sentence)

        return Play()


def run(speaker, **kwargs):
    kwargs.setdefault("poll_seconds", 0.001)
    return engine_common.drain(ROOT, lambda: speaker, OWN, **kwargs)


def put(text, utterance=None, session="S1", sticky=False, engine="kokoro"):
    spool.enqueue(text, engine, "bf_emma", session=session, utterance=utterance, sticky=sticky)
    time.sleep(0.002)


# --- sentence splitting -------------------------------------------------------
check("split after . ! ? and at newlines",
      engine_common.split_sentences("One. Two! Three?\nFour") == ["One.", "Two!", "Three?", "Four"],
      engine_common.split_sentences("One. Two! Three?\nFour"))
check("no split inside a version number",
      engine_common.split_sentences("Version 1.4.0 ships.") == ["Version 1.4.0 ships."])
check("blank text has no sentences", engine_common.split_sentences("  ") == [])

# --- one message plays in order, sentence by sentence -------------------------
spool.clear()
put("First. Second.", utterance="U1")
put("Third.", utterance="U1")
speaker = Recorder()
check("drain exits 0", run(speaker) == 0)
check("a message's items play in order, sentence by sentence",
      speaker.spoken == ["First.", "Second.", "Third."], speaker.spoken)

# --- a newer message waiting at the start skips everything older ---------------
spool.clear()
put("Old one.", utterance="A")
put("Old two.", utterance="B")
put("Newest.", utterance="C")
speaker = Recorder()
run(speaker)
check("only the newest message is spoken", speaker.spoken == ["Newest."], speaker.spoken)
check("the skipped items are gone", spool.pending_count() == 0, spool.pending_count())

# --- a newer message arriving mid-item preempts at the sentence boundary -------
spool.clear()
put("One. Two. Three.", utterance="A")
speaker = Recorder(polls=3, on_begin=lambda s: s == "One." and put("Fresh news.", utterance="B"))
run(speaker)
check("the current sentence finishes, the rest of its message is dropped",
      speaker.spoken == ["One.", "Fresh news."], speaker.spoken)
check("nothing was cut mid-sentence", speaker.killed == [], speaker.killed)

# --- sticky items survive the jump and play whole --------------------------------
spool.clear()
put("Lead-in.", utterance="A")
put("I need your input.", utterance="D", sticky=True)
put("Later text.", utterance="E")
speaker = Recorder()
run(speaker)
check("a sticky alert plays before the newest message",
      speaker.spoken == ["I need your input.", "Later text."], speaker.spoken)
spool.clear()
put("I need your input. Which approach? Options: A, B.", utterance="D", sticky=True)
speaker = Recorder(polls=2, on_begin=lambda s: s == "I need your input." and put("Newer.", utterance="F"))
run(speaker)
check("a sticky alert is not cut by a newer message",
      speaker.spoken == ["I need your input.", "Which approach?", "Options: A, B.", "Newer."],
      speaker.spoken)

# --- two sessions in one project: newest wins ----------------------------------
spool.clear()
put("From session one.", utterance="A", session="S1")
put("From session two.", utterance="B", session="S2")
speaker = Recorder()
run(speaker)
check("the newest message wins across sessions",
      speaker.spoken == ["From session two."], speaker.spoken)

# --- foreign items are neither dropped nor in the way ---------------------------
spool.clear()
put("Theirs.", utterance="X", engine="macos-female")
put("Mine old.", utterance="A")
put("Mine new.", utterance="B")
speaker = Recorder()
run(speaker)
check("own items obey the jump rule", speaker.spoken == ["Mine new."], speaker.spoken)
check("a foreign item keeps its place", spool.pending_count() == 1, spool.pending_count())
spool.clear()

# --- stop requests -----------------------------------------------------------------
put("Long one. Never heard.", utterance="A", session="S1")
speaker = Recorder(polls=50, on_begin=lambda s: s == "Long one." and speech.request_stop(None, "S1"))
run(speaker)
check("a stop for the item's session kills the playing sentence",
      speaker.killed == ["Long one."], speaker.killed)
check("and abandons the rest of the item", speaker.spoken == ["Long one."], speaker.spoken)

spool.clear()
put("Keep going. To the end.", utterance="A", session="S1")
speaker = Recorder(polls=5, on_begin=lambda s: s == "Keep going." and speech.request_stop(None, "S2"))
run(speaker)
check("another session's stop leaves this item alone",
      speaker.spoken == ["Keep going.", "To the end."] and speaker.killed == [],
      (speaker.spoken, speaker.killed))

spool.clear()
put("Anyone's sentence.", utterance="A", session="S3")
speaker = Recorder(polls=50, on_begin=lambda s: speech.request_stop(None, None))
run(speaker)
check("a stop naming no session stops any item", speaker.killed == ["Anyone's sentence."],
      speaker.killed)

spool.clear()
speech.request_stop(None, "S1")
time.sleep(0.01)
put("Queued after the stop.", utterance="A", session="S1")
speaker = Recorder(polls=3)
run(speaker)
check("a stop older than an item never stops it",
      speaker.spoken == ["Queued after the stop."] and speaker.killed == [], speaker.spoken)

spool.clear()
put("Queued before the stop.", utterance="A", session="S1")
time.sleep(0.01)
speech.request_stop(None, "S1")
speaker = Recorder()
run(speaker)
check("an item queued before its session's stop is dropped unplayed",
      speaker.spoken == [], speaker.spoken)

# --- prefetch across items ------------------------------------------------------------
spool.clear()
put("Only one.", utterance="U")
put("Next up. More.", utterance="U")
speaker = Recorder(polls=3)
run(speaker)
check("on an item's last sentence the next item's first sentence is prefetched",
      speaker.prefetched == ["Next up."], speaker.prefetched)

# --- a long message keeps the lock live -------------------------------------------
spool.clear()
put("A. B. C. D.", utterance="A")
alive = []
speaker = Recorder(polls=40, on_begin=lambda s: alive.append(engine_common.lock_is_live(ROOT)))
run(speaker, lock_seconds=0.4, poll_seconds=0.01)  # each sentence plays ~0.4s, past the expiry
check("the lock stays live through playback longer than its expiry",
      alive == [True, True, True, True], alive)

report()
