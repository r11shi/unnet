"""The Ask endpoint is public and spends an API key. These are the limits.

The threat is not clever: a public URL that makes a paid model call per request
gets scripted, and the bill arrives before anyone notices. What is being
asserted here is that the endpoint refuses cheaply, refuses in the right order,
and — the part that matters for a demo — keeps answering the questions it can
answer for free after the paid budget is gone.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from unnet.api import main as api_main
from unnet.api import ratelimit
from unnet.api.ratelimit import AskLimiter


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A client with a fresh limiter, so tests cannot poison each other."""
    monkeypatch.setattr(ratelimit, "ask_limiter", AskLimiter())
    return TestClient(api_main.app)


def _ask(client, text="what is the weather on mars"):
    return client.post("/api/ask", json={"question": text})


# --------------------------------------------------------------------------- #
# The limiter itself
# --------------------------------------------------------------------------- #


def test_the_minute_window_refuses_the_sixth_request():
    limiter = AskLimiter(per_minute=5, per_hour=100)
    assert [limiter.check("1.2.3.4", now=t) for t in (0, 1, 2, 3, 4)] == [None] * 5

    wait = limiter.check("1.2.3.4", now=5)
    assert wait is not None and 0 < wait <= 61


def test_the_minute_window_reopens_once_the_minute_passes():
    limiter = AskLimiter(per_minute=2, per_hour=100)
    limiter.check("1.2.3.4", now=0)
    limiter.check("1.2.3.4", now=1)
    assert limiter.check("1.2.3.4", now=2) is not None

    assert limiter.check("1.2.3.4", now=90) is None


def test_the_hour_window_catches_a_loop_that_paces_itself():
    """A minute limit alone is defeated by waiting a minute."""
    limiter = AskLimiter(per_minute=5, per_hour=10)
    for i in range(10):
        assert limiter.check("1.2.3.4", now=i * 61) is None

    assert limiter.check("1.2.3.4", now=10 * 61) is not None


def test_one_client_hitting_the_wall_does_not_limit_another():
    limiter = AskLimiter(per_minute=1, per_hour=100)
    assert limiter.check("1.1.1.1", now=0) is None
    assert limiter.check("1.1.1.1", now=1) is not None

    assert limiter.check("2.2.2.2", now=1) is None


def test_the_daily_model_budget_is_global_not_per_client():
    """Per-IP limits do not protect a key: addresses are cheap, the key is not."""
    limiter = AskLimiter(model_per_day=3)
    assert [limiter.take_model_call() for _ in range(3)] == [True, True, True]
    assert limiter.take_model_call() is False
    assert limiter.model_calls_left() == 0


def test_the_model_budget_resets_on_a_new_day():
    limiter = AskLimiter(model_per_day=1)
    assert limiter.take_model_call(today=date(2026, 1, 1)) is True
    assert limiter.take_model_call(today=date(2026, 1, 1)) is False

    assert limiter.take_model_call(today=date(2026, 1, 2)) is True


def test_tracked_clients_stay_bounded():
    """The client table is keyed by attacker-chosen input, so it must be capped."""
    limiter = AskLimiter(per_minute=100, per_hour=100)
    for i in range(ratelimit.MAX_TRACKED_CLIENTS + 500):
        limiter.check(f"10.0.{i // 256}.{i % 256}", now=float(i))

    assert len(limiter._hits) <= ratelimit.MAX_TRACKED_CLIENTS


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


def test_an_oversized_question_is_refused_before_anything_is_spent(client):
    response = _ask(client, "why " * 400)

    assert response.status_code == 413
    assert "limit is" in response.json()["detail"]


def test_an_empty_question_is_refused(client):
    assert _ask(client, "   ").status_code == 422


def test_the_rate_limit_answers_429_with_a_retry_after(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "ask_limiter", AskLimiter(per_minute=2))
    for _ in range(2):
        assert _ask(client).status_code != 429

    refused = _ask(client)

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0


def test_a_spent_budget_still_answers_what_the_ledger_can_answer(client, monkeypatch):
    """Degrading beats failing: the free path is what a judge sees at the wall."""
    monkeypatch.setattr(ratelimit, "ask_limiter", AskLimiter(model_per_day=0))

    ledger = client.post("/api/ask", json={"question": "what is still open?"})
    model_only = client.post("/api/ask", json={"question": "list card issuers by volume"})

    assert ledger.status_code == 200
    assert ledger.json()["source"] == "intent"
    assert model_only.status_code == 200
    assert model_only.json()["source"] == "budget_exhausted"


def test_the_client_key_prefers_the_forwarded_address():
    """Behind Render's edge every socket address is the proxy's."""

    class _Req:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert ratelimit.client_key(_Req()) == "203.0.113.9"
