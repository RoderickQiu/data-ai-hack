"""The day clock, and the release schedule it walks (DESIGN §5).

The mechanic that makes an all-day compounding loop possible. Live ATS boards
barely change in a day, so an agent refreshing against them returns an empty
delta and has nothing to compound on. Instead: one static corpus, frozen at
hour 0, with each row stamped with a ``release_day``. Run *n* is day *T_n* and
sees only ``release_day <= T_n``.

Two properties the assignment must have, and both are the reason it is ours
rather than derived from ``posted_at``:

* **Every day carries a non-trivial batch.** Real posting dates give a day with
  four rows and a day with two hundred, which makes the chart lumpy for reasons
  that have nothing to do with the agent.
* **It is deterministic.** The same corpus assigns the same schedule on every
  machine, so a teammate re-running the build gets the same day 7 as everyone
  else, and a play captured against day 7 replays.

The clock does *not* carry the learning proof. If it breaks at hour 4, re-judge
the same day repeatedly and every line on the chart still moves.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Sequence

from agent.config import state_path
from agent.schema import Job

DEFAULT_HORIZON = 30
_CLOCK_FILE = "clock.json"


def assign_release_days(jobs: Sequence[Job], horizon: int = DEFAULT_HORIZON,
                        salt: str = "data-ai-hack") -> list[Job]:
    """Stamp every job with a release day, spread evenly across the horizon.

    Round-robin over a deterministic shuffle rather than ``hash % horizon``: a
    modulus over ids clumps, because ids are not uniformly distributed once a
    board contributes a run of sequential integers. Sorting by a keyed hash and
    dealing the cards gives every day within one row of ``len(jobs)/horizon``,
    and the same deal every time.

    Sources are interleaved on purpose, so no single day is all-Greenhouse and
    the daily batch exercises every normalizer.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")

    def key(job: Job) -> str:
        return hashlib.sha256(f"{salt}|{job.id}".encode()).hexdigest()

    ordered = sorted(jobs, key=key)
    # Deal by source so each day gets a mix of families rather than a block.
    by_source: dict[str, list[Job]] = {}
    for job in ordered:
        by_source.setdefault(job.source, []).append(job)

    interleaved: list[Job] = []
    while any(by_source.values()):
        for source in sorted(by_source):
            bucket = by_source[source]
            if bucket:
                interleaved.append(bucket.pop())

    for index, job in enumerate(interleaved):
        job.release_day = (index % horizon) + 1
    return list(jobs)


def release_histogram(jobs: Iterable[Job]) -> dict[int, int]:
    """Rows per day. The hour-0 check is that no day is trivially small."""
    counts: dict[int, int] = {}
    for job in jobs:
        if job.release_day is not None:
            counts[job.release_day] = counts.get(job.release_day, 0) + 1
    return dict(sorted(counts.items()))


@dataclass
class DayClock:
    """Persisted run counter. One run is one day; ``advance`` is the only writer.

    State is a file rather than a row in ``runs`` on purpose: the clock has to
    answer before the run that produces the row exists, and a run that crashes
    halfway must not silently repeat a day.
    """

    day: int = 1
    horizon: int = DEFAULT_HORIZON
    runs: int = 0

    @classmethod
    def load(cls) -> "DayClock":
        path = state_path(_CLOCK_FILE)
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text()))

    def save(self) -> "DayClock":
        state_path(_CLOCK_FILE).write_text(json.dumps(self.__dict__, indent=2))
        return self

    def advance(self, days: int = 1) -> int:
        """Move the clock on and return the new day.

        Wraps at the horizon rather than running off the end of the corpus: a
        20-run afternoon against a 30-day schedule never wraps, and a longer one
        re-judges rather than returning nothing.
        """
        self.day = ((self.day - 1 + days) % self.horizon) + 1
        self.runs += days
        self.save()
        return self.day

    def set_day(self, day: int) -> int:
        """Pin the clock. Used by the demo, and by 'judge the same day again'."""
        self.day = max(1, min(day, self.horizon))
        self.save()
        return self.day

    def reset(self) -> "DayClock":
        self.day, self.runs = 1, 0
        return self.save()
