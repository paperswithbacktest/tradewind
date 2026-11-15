"""The append-only journal of a live session (spec/05 § 8, spec/07 § 6).

One JSON object per line, written before it takes effect (a command is journalled before it
is sent) and flushed -- by default fsynced -- at once, so a crash loses nothing that
mattered. Record kinds:

* ``session``  first line: session id, symbols, the warm-up history of every feed;
* ``evt``      a message from the adapter, in the order it was applied;
* ``slice``    a slice boundary: its time, the bars that closed, whether it was a catch-up;
* ``cmd``      an order sent or a cancel asked, with the slice it came from;
* ``recon``, ``guard``, ``kill``, ``note``: what reconciliation, the guards and the kill
  switch did (informational; the replay recomputes them).

Python's ``json`` writes a float with ``repr``, which reads back to the same bits: a replay
sees exactly the prices the session saw.
"""

from __future__ import annotations

import json
import os
import time


class Journal:
    """Append records to ``path`` (or only to memory when ``path`` is None)."""

    def __init__(self, path: str | os.PathLike | None = None, fsync: bool = True, keep: bool = True):
        self.path = None if path is None else os.fspath(path)
        self.fsync = fsync
        self.records: list[dict] = [] if keep else None
        self._fh = None
        self._n = 0

    # -- reading ---------------------------------------------------------------------------
    @staticmethod
    def load(path: str | os.PathLike) -> list[dict]:
        """Every complete record of a journal; a torn last line (a crash mid-write) is dropped."""
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    break
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def existing(self) -> list[dict]:
        if self.path is None or not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return []
        return self.load(self.path)

    # -- writing ---------------------------------------------------------------------------
    def open(self) -> None:
        if self.path is not None and self._fh is None:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._repair()
            self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - held for the session

    def _repair(self) -> None:
        """Cut a torn last line before appending after it."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb+") as fh:
            data = fh.read()
            if data and not data.endswith(b"\n"):
                fh.truncate(data.rfind(b"\n") + 1)

    def write(self, kind: str, **payload) -> dict:
        self._n += 1
        record = {"k": kind, "wall": time.time_ns()}
        record.update(payload)
        if self.records is not None:
            self.records.append(record)
        if self.path is not None:
            if self._fh is None:
                self.open()
            self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._fh.flush()
            if self.fsync:
                os.fsync(self._fh.fileno())
        return record

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


__all__ = ["Journal"]
