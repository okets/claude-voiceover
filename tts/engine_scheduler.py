# tts/engine_scheduler.py
"""Round-robin sentence scheduler for the shared Kokoro engine. Stdlib only.

Jobs within a connection are strictly first-in, first-out. Scheduling works in
rounds: each round serves, once, every connection that had a ready job when the
round began. Within a round, connections whose head job is live go first;
otherwise the least recently served goes first (never served before any). A connection
served this round waits for the next one, so a narration sentence never waits
behind more than one sentence of any other caller.
"""

from collections import deque
from itertools import count
from typing import NamedTuple


class Job(NamedTuple):
    conn: int
    req: str
    text: str
    live: bool


class Scheduler:
    def __init__(self):
        self._queues = {}        # conn -> deque of Job (non-empty only)
        self._last_served = {}   # conn -> ticket of its latest served job (absent: never served)
        self._tickets = count()
        self._round = []         # conns still to be served this round

    def add(self, job: Job) -> None:
        if job.conn not in self._queues:
            self._queues[job.conn] = deque()
        self._queues[job.conn].append(job)

    def next(self, exclude=()):
        """The next job to synthesize, or None when nothing is queued.

        Connections in `exclude` are skipped for this pick and keep their place."""
        conn = self._next_conn(exclude)
        if conn is None:
            return None
        job = self._queues[conn].popleft()
        self._served(conn)
        return job

    def cancel(self, conn: int, req=None) -> int:
        """Drop a connection's queued jobs (one request's, or all). Returns how many."""
        queue = self._queues.get(conn)
        if queue is None:
            return 0
        kept = deque(j for j in queue if req is not None and j.req != req)
        dropped = len(queue) - len(kept)
        self._replace(conn, kept)
        return dropped

    def has_work(self) -> bool:
        return bool(self._queues)

    def pending(self, conn: int) -> int:
        return len(self._queues.get(conn, ()))

    # --- internals ---------------------------------------------------------

    def _next_conn(self, exclude):
        """Serve the current round; when it holds no eligible connection, start a fresh one."""
        for _ in range(2):
            self._round = [c for c in self._round if c in self._queues]
            pick = next((c for c in self._round if c not in exclude), None)
            if pick is not None:
                self._round.remove(pick)
                return pick
            self._round = self._ordered_conns()
        return None

    def _ordered_conns(self):
        """Live heads first, then least recently served (never served first)."""
        return sorted(self._queues, key=lambda c: (not self._queues[c][0].live,
                                                   self._last_served.get(c, -1)))

    def _served(self, conn):
        """After serving: note when, and drop an emptied connection."""
        self._last_served[conn] = next(self._tickets)
        if not self._queues[conn]:
            self._forget(conn)

    def _replace(self, conn, queue):
        if queue:
            self._queues[conn] = queue
        else:
            self._forget(conn)

    def _forget(self, conn):
        self._queues.pop(conn, None)
        self._last_served.pop(conn, None)
