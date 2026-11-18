# tests/test_engine_text.py
"""Incremental sentence text: buffering, long-sentence splitting, live first clause."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from engine_text import UtteranceText, split_long

u = UtteranceText()
check("partial held back", u.feed("The build passed. I am now run") == ["The build passed."])
check("fragment joins", u.feed("ning the tests") == [] and u.drain() == ["I am now running the tests"])
check("no punctuation spoken on drain", UtteranceText().feed("ok") == [])
w = UtteranceText(); w.feed("ok"); check("drain gives ok", w.drain() == ["ok"])
ws = UtteranceText(); check("whitespace only -> nothing", ws.feed("   ") == [] and ws.drain() == [])
check("drain empties the buffer", w.drain() == [])
nl = UtteranceText()
check("a newline completes a sentence", nl.feed("one\ntwo") == ["one"] and nl.drain() == ["two"])
cc = UtteranceText(); cc.feed("héllo "); cc.feed("wörld")
check("chars counts everything received", cc.chars == len("héllo ") + len("wörld"))

long = " ".join(["word"] * 30) + ", " + " ".join(["more"] * 30) + "."
check("long sentence split at clause", all(len(p.split()) <= 40 for p in split_long(long)) and len(split_long(long)) == 2)
check("no boundary -> 40-word cuts", [len(p.split()) for p in split_long(" ".join(["w"] * 90))] == [40, 40, 10])
check("short sentence untouched", split_long("a, b, c") == ["a, b, c"])
nearest = " ".join(["a"] * 5) + "; " + " ".join(["b"] * 25) + ": " + " ".join(["c"] * 25)
check("splits at the boundary nearest the middle",
      [len(p.split()) for p in split_long(nearest)] == [30, 25])
dash = " ".join(["a"] * 30) + " — " + " ".join(["b"] * 30)
check("em dash is a boundary", len(split_long(dash)) == 2)
check("the sentence's own punctuation is kept", "".join(split_long(long)).replace(" ", "") == long.replace(" ", ""))
big = UtteranceText()
check("feed applies the long-sentence split", len(big.feed(" ".join(["w"] * 90) + ". ")) == 3)

lv = UtteranceText(live=True)
check("live first clause early (first boundary after >= 4 words)",
      lv.feed("Well, sure thing, I can check the build now. Next. ") ==
      ["Well, sure thing,", "I can check the build now.", "Next."])
lv2 = UtteranceText(live=True); lv2.feed("First. ")
check("live split is for the first sentence only", lv2.feed("Well, sure thing, I can check. ") ==
      ["Well, sure thing, I can check."])
check("non-live first sentence not split", UtteranceText().feed("Well, sure thing, I can check the build now. ") ==
      ["Well, sure thing, I can check the build now."])

report()
