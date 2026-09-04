"""Model access: one interface, free-tier providers, and a way to run with none.

Three things this has to survive, in order of how often they happen:

1. **No API key at all.** A reviewer clones the repo and runs ``make demo``.
   Cassette replay serves recorded responses so the published numbers reproduce
   byte for byte with no account and no network.
2. **Free-tier rate limits.** Gemini and Groq both return 429 under load. A
   reconciliation run that dies two thirds of the way through because a free
   quota ran out is worse than one that finishes on rules alone, so the circuit
   breaker trips and the run degrades instead of failing.
3. **A model returning nonsense.** Handled downstream by the verifier, not here.
   This layer's only job is to return parsed JSON or admit it could not.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

CASSETTE_DIR = Path(os.environ.get("UNNET_CASSETTES", "data/cassettes"))


class LLMUnavailable(RuntimeError):
    """Raised when no model can serve a request. Callers degrade, not crash."""


#: Query parameters that carry a credential. Gemini takes the API key in the
#: URL, so httpx's own error text — "Server error '503' for url
#: https://...:generateContent?key=AQ.Ab8RN..." — contains it verbatim.
_SECRET_PARAMS = ("key", "api_key", "apikey", "access_token", "token")
_SECRET_RE = re.compile(
    r"([?&](?:" + "|".join(_SECRET_PARAMS) + r")=)[^&\s'\"]+",
    re.IGNORECASE,
)


def redact(text: object) -> str:
    """Strip credentials out of anything on its way to a store or a screen.

    A provider error is not a private object: it is written to
    ``exception.verifier_reason``, persisted in SQLite, served by the API and
    rendered on the case detail page. One 503 from Google was enough to put a
    live API key in all four of those places. Redaction belongs here, at the
    boundary where the error is first turned into our own text, rather than at
    each of the places it later travels to.
    """
    out = _SECRET_RE.sub(r"\1[redacted]", str(text))
    # Bearer tokens, for a backend that sends the credential in a header and
    # echoes the request back.
    return re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1[redacted]", out)


@dataclass
class LLMResponse:
    data: dict[str, Any]
    source: str  # "cassette" | "gemini" | "groq"
    model: str
    prompt_hash: str
    latency_ms: int = 0

    @property
    def decider_ref(self) -> str:
        """Identifier recorded in the audit trail.

        The prompt hash is included so a decision can be traced back to the
        exact input that produced it — a model name alone is not reproducible.
        """
        return f"{self.source}:{self.model}:{self.prompt_hash[:12]}"


class Backend(Protocol):
    name: str
    model: str
    #: Tokens used by the most recent call, when the provider reports them.
    last_tokens: int

    def complete(self, prompt: str, schema: dict) -> dict: ...


class RateLimiter:
    """Token bucket, because the free tier is 10 requests per minute.

    Ten RPM is slow enough to be an architectural constraint rather than a
    footnote: it is why the investigator generates candidates deterministically
    first and only spends a call where reasoning is actually required. Pacing
    here beats collecting 429s and tripping the circuit breaker on what is
    really a queueing problem.
    """

    def __init__(self, per_minute: int | None = None) -> None:
        self.per_minute = per_minute or int(os.environ.get("UNNET_LLM_RPM", "10"))
        self.interval = 60.0 / max(1, self.per_minute)
        self._last: float = 0.0
        self.waited_seconds: float = 0.0

    def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        earliest = self._last + self.interval
        if now < earliest:
            delay = earliest - now
            self.waited_seconds += delay
            time.sleep(delay)
        self._last = time.monotonic()


def prompt_hash(task: str, prompt: str) -> str:
    return hashlib.sha256(f"{task}\n{prompt}".encode()).hexdigest()


class CassetteStore:
    """Recorded responses, keyed by task and prompt hash.

    Committed to the repo so the published metrics are reproducible offline.
    A cassette is a record of what a model actually returned — never a
    hand-written answer, which would make the metrics a work of fiction.
    """

    def __init__(self, directory: Path = CASSETTE_DIR) -> None:
        self.directory = Path(directory)

    def path_for(self, task: str, digest: str) -> Path:
        return self.directory / task / f"{digest[:32]}.json"

    def load(self, task: str, digest: str) -> Optional[dict]:
        path = self.path_for(task, digest)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def save(self, task: str, digest: str, payload: dict, meta: dict) -> None:
        path = self.path_for(task, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"meta": meta, "data": payload}, indent=2))

    def count(self) -> int:
        return len(list(self.directory.rglob("*.json"))) if self.directory.exists() else 0


@dataclass
class LLMClient:
    """What the agents actually talk to."""

    backends: list[Backend] = field(default_factory=list)
    cassettes: CassetteStore = field(default_factory=CassetteStore)
    record: bool = False
    #: Consecutive failures before this run stops trying.
    breaker_threshold: int = 3
    limiter: RateLimiter = field(default_factory=RateLimiter)

    calls: int = 0
    cassette_hits: int = 0
    live_calls: int = 0
    failures: int = 0
    tokens: int = 0
    _consecutive_failures: int = 0
    degraded: bool = False
    degraded_reason: str = ""

    def complete(self, task: str, prompt: str, schema: dict) -> LLMResponse:
        """Serve from cassette, else from a live backend, else give up cleanly."""
        self.calls += 1
        digest = prompt_hash(task, prompt)

        cached = self.cassettes.load(task, digest)
        if cached is not None:
            self.cassette_hits += 1
            meta = cached.get("meta", {})
            # Replay the cost the live call actually incurred. Reporting a
            # cassette run as zero-token would make the AI layer look free.
            self.tokens += int(meta.get("tokens") or 0)
            return LLMResponse(
                data=cached.get("data", {}),
                source="cassette",
                model=meta.get("model", "recorded"),
                prompt_hash=digest,
            )

        if self.degraded:
            raise LLMUnavailable(self.degraded_reason)

        if not self.backends:
            raise LLMUnavailable(
                "No cassette for this prompt and no API key configured. "
                "Set GEMINI_API_KEY or GROQ_API_KEY, or run `make record`."
            )

        last_error: Exception | None = None
        for backend in self.backends:
            # Pace before the call, not after a 429, so the free tier is a
            # queue rather than a failure mode.
            self.limiter.acquire()
            started = time.perf_counter()
            try:
                data = backend.complete(prompt, schema)
            except Exception as exc:  # noqa: BLE001 - any backend failure is a fallback
                last_error = exc
                self.failures += 1
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            self.live_calls += 1
            self._consecutive_failures = 0
            call_tokens = int(getattr(backend, "last_tokens", 0) or 0)
            self.tokens += call_tokens

            if self.record:
                self.cassettes.save(
                    task,
                    digest,
                    data,
                    {
                        "model": getattr(backend, "last_model", None) or backend.model,
                        "backend": backend.name,
                        "task": task,
                        "latency_ms": latency_ms,
                        "tokens": call_tokens,
                    },
                )

            return LLMResponse(
                data=data,
                source=backend.name,
                model=getattr(backend, "last_model", None) or backend.model,
                prompt_hash=digest,
                latency_ms=latency_ms,
            )

        # Every backend failed for this prompt.
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.breaker_threshold:
            self.degraded = True
            self.degraded_reason = (
                f"Circuit breaker tripped after {self._consecutive_failures} consecutive "
                f"failures; continuing on rules only. Last error: {redact(last_error)}"
            )
        raise LLMUnavailable(redact(last_error) if last_error else "all backends failed")

    def stats(self) -> dict:
        return {
            "calls": self.calls,
            "cassette_hits": self.cassette_hits,
            "live_calls": self.live_calls,
            "failures": self.failures,
            "tokens": self.tokens,
            "rate_limit_wait_s": round(self.limiter.waited_seconds, 1),
            "rpm_cap": self.limiter.per_minute,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }


def build_client(
    *,
    provider: str | None = None,
    record: bool | None = None,
    cassette_dir: Path | None = None,
) -> LLMClient:
    """Assemble a client from the environment.

    ``offline`` means cassettes only — the default, so nothing in this repo
    reaches the network unless someone asks it to.
    """
    provider = (provider or os.environ.get("UNNET_LLM_PROVIDER", "offline")).lower()
    record = record if record is not None else os.environ.get("UNNET_LLM_RECORD") == "1"
    store = CassetteStore(cassette_dir or CASSETTE_DIR)

    backends: list[Backend] = []
    # Order is the fallback order. A local server is tried first when asked for
    # explicitly, since it costs nothing and has no quota to exhaust.
    if provider in {"local", "auto"}:
        from unnet.llm.local import LocalBackend

        if provider == "local" or os.environ.get("UNNET_LOCAL_BASE_URL"):
            backends.append(LocalBackend())
    if provider in {"gemini", "auto"} and os.environ.get("GEMINI_API_KEY"):
        from unnet.llm.gemini import GeminiBackend

        backends.append(GeminiBackend())
    if provider in {"groq", "auto"} and os.environ.get("GROQ_API_KEY"):
        from unnet.llm.groq import GroqBackend

        backends.append(GroqBackend())

    return LLMClient(backends=backends, cassettes=store, record=record)
