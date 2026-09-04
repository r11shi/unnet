"""What stops a public Ask endpoint from becoming a public LLM proxy.

`/api/ask` is deliberately unauthenticated: the point of the deployed demo is
that a reviewer can ask the system a question without being handed a token
first. That decision is only defensible with a budget attached, because the
natural-language path spends a real API key on every call, and a URL that
spends someone's key on request is a URL that gets scripted.

Three limits, smallest blast radius first:

* **Per IP, per minute** — stops one browser (or one loop) monopolising it.
* **Per IP, per hour** — stops a patient loop that respects the minute.
* **Model calls per day, globally** — the only one that actually protects the
  key, because the two above are per-IP and IPs are cheap.

The daily budget covers *model* answers only. The deterministic intent
answers cost nothing to serve, so they stay unlimited: when the budget is gone
the endpoint keeps answering the questions it can answer for free and says
plainly that the natural-language path is paused. Degrading beats 500ing, and
a finance tool that goes silent under load is worse than one that narrows.

In-process state, deliberately. A single free-tier instance has one process,
so a dict is the correct amount of machinery; Redis here would be
infrastructure bought to solve a problem this deployment does not have. It
follows that the counters reset when the instance restarts, which is
acceptable for a spend cap whose real ceiling is the provider's own quota.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import date

#: Ask is a question box, not an upload. Anything past this is a cost attack
#: rather than a question, and the model is billed by the token.
MAX_QUESTION_CHARS = int(os.environ.get("UNNET_ASK_MAX_CHARS", "500"))

PER_MINUTE = int(os.environ.get("UNNET_ASK_PER_MIN", "5"))
PER_HOUR = int(os.environ.get("UNNET_ASK_PER_HOUR", "40"))
MODEL_PER_DAY = int(os.environ.get("UNNET_ASK_MODEL_PER_DAY", "150"))

#: Past this many tracked clients we drop the ones with no recent activity.
#: Without it the dict is an unbounded allocation keyed by attacker-chosen
#: input, which is its own denial of service.
MAX_TRACKED_CLIENTS = 2048


class AskLimiter:
    """Per-IP request limits plus one global budget for paid model calls."""

    def __init__(
        self,
        *,
        per_minute: int = PER_MINUTE,
        per_hour: int = PER_HOUR,
        model_per_day: int = MODEL_PER_DAY,
    ) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.model_per_day = model_per_day
        self._hits: dict[str, deque[float]] = {}
        self._model_day: date | None = None
        self._model_used = 0
        self._lock = threading.Lock()

    # -- per client ------------------------------------------------------- #

    def check(self, client: str, *, now: float | None = None) -> int | None:
        """Record a request. Returns seconds to wait if it should be refused.

        The same deque answers both windows: the minute limit reads its tail,
        the hour limit its length. One structure, so the two can never
        disagree about what happened.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(client)
            if hits is None:
                hits = self._hits.setdefault(client, deque())
                if len(self._hits) > MAX_TRACKED_CLIENTS:
                    self._evict(now)

            while hits and now - hits[0] > 3600:
                hits.popleft()

            in_last_minute = sum(1 for t in hits if now - t <= 60)
            if in_last_minute >= self.per_minute:
                oldest = next(t for t in hits if now - t <= 60)
                return max(1, int(61 - (now - oldest)))
            if len(hits) >= self.per_hour:
                return max(1, int(3601 - (now - hits[0])))

            hits.append(now)
            return None

    def _evict(self, now: float) -> None:
        """Drop clients with nothing inside the hour. Caller holds the lock."""
        stale = [k for k, v in self._hits.items() if not v or now - v[-1] > 3600]
        for key in stale:
            del self._hits[key]
        if len(self._hits) > MAX_TRACKED_CLIENTS:
            # Everyone is active. Forget the least recently seen rather than
            # grow without bound; they simply get a fresh allowance.
            for key in sorted(self._hits, key=lambda k: self._hits[k][-1])[:len(self._hits) // 4]:
                del self._hits[key]

    # -- global model budget ---------------------------------------------- #

    def take_model_call(self, *, today: date | None = None) -> bool:
        """Claim one paid call from today's budget. False when it is spent."""
        today = date.today() if today is None else today
        with self._lock:
            if self._model_day != today:
                self._model_day = today
                self._model_used = 0
            if self._model_used >= self.model_per_day:
                return False
            self._model_used += 1
            return True

    def model_calls_left(self, *, today: date | None = None) -> int:
        today = date.today() if today is None else today
        with self._lock:
            if self._model_day != today:
                return self.model_per_day
            return max(0, self.model_per_day - self._model_used)


#: One limiter for the process. Tests build their own.
ask_limiter = AskLimiter()


def client_key(request) -> str:
    """Best available identity for a caller.

    Render terminates TLS at its edge and forwards the original address in
    `X-Forwarded-For`, so the socket address is the proxy for every request and
    is useless as a key. The leftmost entry is the client as the edge saw it.
    A determined attacker can rotate addresses regardless — which is exactly
    why the daily model budget, the limit that actually protects the key, is
    global rather than per client.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]
