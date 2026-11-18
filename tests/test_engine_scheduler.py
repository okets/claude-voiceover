# tests/test_engine_scheduler.py
"""Round-robin sentence scheduler: per-connection FIFO, live priority within a round."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import check, isolate, report, REPO_ROOT

isolate()
sys.path.insert(0, str(REPO_ROOT / "tts"))
from engine_scheduler import Job, Scheduler


def job(conn, req="r", text="t", live=False):
    return Job(conn, req, text, live)


def drain(scheduler):
    out = []
    while True:
        item = scheduler.next()
        if item is None:
            return out
        out.append(item)


# --- fairness ---------------------------------------------------------------
s = Scheduler()
for i in range(3):
    s.add(job("A", text="a%d" % i))
for i in range(2):
    s.add(job("B", text="b%d" % i))
check("A, B alternate", [j.text for j in drain(s)] == ["a0", "b0", "a1", "b1", "a2"])
check("empty scheduler has no work", not s.has_work() and s.next() is None)

# --- a connection is FIFO ---------------------------------------------------
s = Scheduler()
for i in range(3):
    s.add(job("A", text=str(i)))
check("FIFO within a connection", [j.text for j in drain(s)] == ["0", "1", "2"])

# --- a live connection joins the next round first ---------------------------
s = Scheduler()
for i in range(3):
    s.add(job("A", text="a%d" % i))
    s.add(job("B", text="b%d" % i))
first = s.next()                 # round 1 is [A, B]; A is served
s.add(job("C", text="c0", live=True))
rest = [j.text for j in drain(s)]
check("the round in progress finishes before C", first.text == "a0" and rest[0] == "b0", rest)
check("the next round starts with the live connection", rest[1] == "c0", rest)

# --- narration never waits more than one sentence behind live ---------------
s = Scheduler()
for i in range(5):
    s.add(job("L", text="L", live=True))
for i in range(2):
    s.add(job("N", text="N"))
check("L, N, L, N, L, L, L", "".join(j.text for j in drain(s)) == "LNLNLLL")

# --- cancel -----------------------------------------------------------------
s = Scheduler()
s.add(job("A", "r1", "x")); s.add(job("A", "r2", "y")); s.add(job("A", "r1", "z")); s.add(job("B", "r1", "w"))
check("cancel by request returns the count", s.cancel("A", "r1") == 2)
check("only that request's jobs go", [j.text for j in drain(s)] == ["y", "w"])
s = Scheduler()
s.add(job("A", "r1")); s.add(job("A", "r2")); s.add(job("B", "r1"))
check("cancel a whole connection", s.cancel("A") == 2 and s.pending("A") == 0 and s.pending("B") == 1)
check("cancelling nothing returns 0", s.cancel("zzz") == 0)
s = Scheduler(); s.add(job("A")); s.add(job("B")); s.next()
s.cancel("B")
check("a cancelled connection is skipped mid-round", s.next() is None and not s.has_work())

check("pending counts queued jobs", (lambda q: (q.add(job("A")), q.add(job("A")), q.pending("A"))[2])(Scheduler()) == 2)

# --- exclude: skipped for this pick, place kept ----------------------------------------------
s = Scheduler(); s.add(job("A", text="a1")); s.add(job("B", text="b1")); s.add(job("A", text="a2"))
check("an excluded connection is skipped", s.next(exclude={"A"}).text == "b1")
check("with only excluded work, next is None and the work stays", s.next(exclude={"A"}) is None and s.pending("A") == 2)
check("an excluded connection keeps its place once allowed again", [j.text for j in drain(s)] == ["a1", "a2"])
s = Scheduler(); s.add(job("A", text="a1")); s.add(job("B", text="b1"))
s.next(exclude={"A"})
s.add(job("C", text="c1"))
check("newcomers are served while another connection stays excluded", s.next(exclude={"A"}).text == "c1")
s = Scheduler(); s.add(job("A", text="a1")); s.add(job("B", text="b1"))
check("exclude does not reorder the others", [s.next(exclude={"A"}).text, s.next().text] == ["b1", "a1"])

# --- a newcomer is not queued behind a busy caller's next sentence (M17, ruling R16) -------------
s = Scheduler()
for i in range(10):
    s.add(job("A", text="a%d" % i))
in_flight = s.next()
s.add(job("B", text="b0"))
check("probe: the in-flight pick is A's first", in_flight.text == "a0")
check("probe: the newcomer goes next, then A", [s.next().text, s.next().text] == ["b0", "a1"])
s = Scheduler()
s.add(job("A", text="a0")); s.add(job("A", text="a1")); s.next()
s.add(job("B", text="b0", live=False)); s.add(job("C", text="c0", live=True))
check("live heads still go first, among the least recently served", s.next().text == "c0")

s = Scheduler()
for i in range(2):
    s.add(job("A", text="a%d" % i)); s.add(job("B", text="b%d" % i, live=True))
check("setup: A is served before B", [s.next(exclude={"B"}).text, s.next().text] == ["a0", "b0"])
check("a live head beats an older-served non-live connection", [s.next().text, s.next().text] == ["b1", "a1"])

report()
