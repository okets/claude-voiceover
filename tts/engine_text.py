# tts/engine_text.py
"""Incremental text -> sentences for the shared Kokoro engine. Stdlib only.

Sentence rule: engine_common.split_sentences (M8). On top of it, sentences over
CLAUSE_SPLIT_WORDS words split at clause boundaries (D7), and a live
utterance's first sentence is split early at its first clause boundary so first
audio does not wait for a whole sentence to render.
"""

import re

from engine_common import split_sentences

CLAUSE_SPLIT_WORDS = 40
LIVE_FIRST_CLAUSE_MIN_WORDS = 4

# Where a clause may end: after , ; : followed by whitespace, after " -", after an em dash.
_BOUNDARY = re.compile(r"[,;:](?=\s)|(?<=\s)-(?=\s)|—")


def _word_count(text):
    return len(text.split())


def _boundaries(sentence):
    """(head, tail) pairs, one per clause boundary, both sides non-empty."""
    pairs = []
    for match in _BOUNDARY.finditer(sentence):
        head, tail = sentence[:match.end()].strip(), sentence[match.end():].strip()
        if head and tail:
            pairs.append((head, tail))
    return pairs


def _cut_words(sentence, max_words):
    """No boundary to use: cut after max_words words."""
    words = sentence.split()
    return " ".join(words[:max_words]), " ".join(words[max_words:])


def _split_point(sentence, max_words):
    middle = _word_count(sentence) / 2
    pairs = _boundaries(sentence)
    if not pairs:
        return _cut_words(sentence, max_words)
    return min(pairs, key=lambda pair: abs(_word_count(pair[0]) - middle))


def split_long(sentence, max_words=CLAUSE_SPLIT_WORDS):
    """A sentence -> pieces of at most max_words words, cut at the clause
    boundary nearest the middle, else at max_words words."""
    if _word_count(sentence) <= max_words:
        return [sentence]
    head, tail = _split_point(sentence, max_words)
    return split_long(head, max_words) + split_long(tail, max_words)


def _first_clause(sentence):
    """(head, tail) at the first boundary that has read LIVE_FIRST_CLAUSE_MIN_WORDS
    words by the time it is passed (the word after the boundary counts), else None."""
    for head, tail in _boundaries(sentence):
        if _word_count(head) + 1 >= LIVE_FIRST_CLAUSE_MIN_WORDS:
            return head, tail
    return None


class UtteranceText:
    """Turns streamed text into sentences as they complete."""

    def __init__(self, live=False):
        self._live = live
        self._buffer = ""
        self._first_pending = True
        self.chars = 0

    def feed(self, text):
        """Add text; return the sentences now complete. The trailing partial waits."""
        self.chars += len(text)
        self._buffer += text
        *complete, self._buffer = re.split(r"(?<=[.!?])\s+|\n+", self._buffer)
        return self._emit(complete)

    def drain(self):
        """Everything still buffered (flush or end)."""
        rest, self._buffer = self._buffer, ""
        return self._emit([rest])

    def _emit(self, raw_sentences):
        out = []
        for sentence in split_sentences("\n".join(raw_sentences)):
            out.extend(self._pieces(sentence))
        return out

    def _pieces(self, sentence):
        if self._first_pending:
            self._first_pending = False
            clause = _first_clause(sentence) if self._live else None
            if clause:
                return [piece for part in clause for piece in split_long(part)]
        return split_long(sentence)
