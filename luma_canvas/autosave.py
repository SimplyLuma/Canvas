# SPDX-License-Identifier: MPL-2.0
"""When Canvas saves. Pure timing rules with an injected clock.

CANVAS-SPEC 13.3: a save 1.5 s after the last committed change, and at most
10 s after the first unsaved change while edits keep arriving. Focus loss,
close, quit and session end save immediately; those are the window's calls,
not this scheduler's. A failed save retries after 10 s, backing off to 60 s.
Every 10 minutes of editing time a version is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEBOUNCE = 1.5
MAX_INTERVAL = 10.0
RETRY_DELAYS = (10.0, 20.0, 40.0, 60.0)
VERSION_EVERY = 600.0
# Gaps between edits longer than this are thinking, not editing time.
EDITING_GAP = 60.0


@dataclass
class SaveScheduler:
    debounce: float = DEBOUNCE
    max_interval: float = MAX_INTERVAL
    first_unsaved: float | None = None
    last_change: float | None = None
    failures: int = 0
    editing_seconds: float = 0.0
    _version_mark: float = field(default=0.0)

    @property
    def dirty(self) -> bool:
        return self.last_change is not None

    def changed(self, now: float) -> float:
        """Record a committed change; return when the save is due."""
        if self.last_change is not None:
            self.editing_seconds += min(EDITING_GAP, max(0.0, now - self.last_change))
        if self.first_unsaved is None:
            self.first_unsaved = now
        self.last_change = now
        return self.deadline()

    def deadline(self) -> float | None:
        if self.last_change is None or self.first_unsaved is None:
            return None
        return min(self.last_change + self.debounce, self.first_unsaved + self.max_interval)

    def due(self, now: float) -> bool:
        deadline = self.deadline()
        return deadline is not None and now >= deadline - 1e-6

    def started_save(self) -> tuple[float | None, float | None]:
        """A save is taking the current state; changes after this are new."""
        marks = (self.first_unsaved, self.last_change)
        self.first_unsaved = None
        self.last_change = None
        return marks

    def save_succeeded(self) -> None:
        self.failures = 0

    def save_failed(self, marks: tuple[float | None, float | None], now: float) -> float:
        """Put the unsaved state back and return when to retry."""
        first, last = marks
        if first is not None:
            self.first_unsaved = first if self.first_unsaved is None else min(first, self.first_unsaved)
        if last is not None and (self.last_change is None or last > self.last_change):
            self.last_change = last
        delay = RETRY_DELAYS[min(self.failures, len(RETRY_DELAYS) - 1)]
        self.failures += 1
        return now + delay

    def version_due(self) -> bool:
        """True once per 10 minutes of editing time."""
        if self.editing_seconds - self._version_mark >= VERSION_EVERY:
            self._version_mark = self.editing_seconds
            return True
        return False
