"""Reading the advisor's answer while the model is still writing it.

The model replies with one JSON object whose first field is "answer". While
it streams, AnswerTap picks the text of that field out of the partial JSON --
escapes decoded, a half-arrived escape held back until the rest comes -- so
the reader sees words, never braces and quotes. What they see is a draft: the
finished reply is validated and cleaned as before, and replaces it.
"""
from __future__ import annotations

import re
import threading


class Cancel:
    """A stop signal that can be given from another thread.

    The stream reading the model registers how to abort itself (closing its
    HTTP response); `set()` runs that at once, from whichever thread noticed
    the reader has gone. A closer registered after `set()` runs immediately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closers: list = []
        self.is_set = False

    def on(self, closer) -> None:
        with self._lock:
            if not self.is_set:
                self._closers.append(closer)
                return
        _quietly(closer)

    def set(self) -> None:
        with self._lock:
            if self.is_set:
                return
            self.is_set = True
            closers, self._closers = self._closers, []
        for closer in closers:
            _quietly(closer)


def _quietly(closer) -> None:
    try:
        closer()
    except Exception:                                  # noqa: BLE001 - aborting must not raise
        pass

_START = re.compile(r'"answer"\s*:\s*"')
_ESCAPES = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", '"': '"', "\\": "\\", "/": "/"}


class AnswerTap:
    """Feed it the raw pieces; it returns the new answer text in each."""

    def __init__(self) -> None:
        self.raw = ""
        self._at: int | None = None       # where the answer's text resumes
        self.finished = False

    def feed(self, piece: str) -> str:
        self.raw += piece or ""
        if self.finished:
            return ""
        if self._at is None:
            match = _START.search(self.raw)
            if match is None:
                return ""
            self._at = match.end()

        raw, i, out = self.raw, self._at, []
        while i < len(raw):
            char = raw[i]
            if char == '"':
                self.finished = True
                i += 1
                break
            if char != "\\":
                out.append(char)
                i += 1
                continue
            if i + 1 >= len(raw):
                break                                  # the escape's second half is still coming
            kind = raw[i + 1]
            if kind != "u":
                out.append(_ESCAPES.get(kind, kind))
                i += 2
                continue
            if i + 6 > len(raw):
                break
            try:
                code = int(raw[i + 2:i + 6], 16)
            except ValueError:
                i += 6
                continue
            if 0xD800 <= code < 0xDC00:                # half of a pair: wait for the other half
                if i + 12 > len(raw):
                    break
                try:
                    low = int(raw[i + 8:i + 12], 16) if raw[i + 6:i + 8] == "\\u" else None
                except ValueError:
                    low = None
                if low is not None and 0xDC00 <= low < 0xE000:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    i += 12
                    continue
            out.append(chr(code))
            i += 6
        self._at = i
        return "".join(out)
