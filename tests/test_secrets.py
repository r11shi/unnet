"""A provider error is not a private object.

It lands in ``exception.verifier_reason``, is committed to SQLite, served by
`/api/cases/{key}` and rendered on the case detail page. Gemini takes its API
key as a URL query parameter, so httpx's own error text carries the key
verbatim — one 503 from Google was enough to put a live credential in all four
of those places, which is how these tests came to exist.
"""

from __future__ import annotations

from unnet.llm.provider import redact

KEY = "AQ.Ab8RN6LLnotarealkey0000000000000000"


def test_a_gemini_url_error_does_not_carry_the_key():
    raw = (
        "Server error '503 Service Unavailable' for url "
        f"'https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.6-flash:generateContent?key={KEY}'"
    )
    out = redact(raw)

    assert KEY not in out
    assert "key=[redacted]" in out
    # The diagnostic has to survive, or redaction just trades one problem for
    # an unfixable outage.
    assert "503" in out and "generateContent" in out


def test_other_credential_shapes_are_covered():
    out = redact(f"POST /v1?api_key={KEY}&model=x")
    assert KEY not in out and "model=x" in out

    out = redact(f"headers: Authorization: Bearer {KEY}")
    assert KEY not in out and "Bearer [redacted]" in out

    out = redact(f"https://x/y?access_token={KEY}")
    assert KEY not in out


def test_redaction_leaves_ordinary_errors_alone():
    plain = "The read operation timed out"
    assert redact(plain) == plain
    assert redact(None) == "None"


def test_the_unavailable_error_a_caller_sees_is_already_redacted():
    """Redacting at the raise site, not at each of the places it travels to."""
    from unnet.llm.provider import LLMClient, LLMUnavailable

    class Exploding:
        name, model = "gemini", "gemini-3.6-flash"

        def complete(self, prompt, schema):
            raise RuntimeError(
                f"Server error '503' for url 'https://x/y:generateContent?key={KEY}'"
            )

    client = LLMClient(backends=[Exploding()])
    try:
        client.complete("exception_triage", "prompt that has no cassette", {})
    except LLMUnavailable as exc:
        assert KEY not in str(exc)
        assert "[redacted]" in str(exc)
    else:  # pragma: no cover - the backend always raises
        raise AssertionError("expected LLMUnavailable")


def test_the_circuit_breaker_reason_is_redacted_too():
    from unnet.llm.provider import LLMClient, LLMUnavailable

    class Exploding:
        name, model = "gemini", "gemini-3.6-flash"

        def complete(self, prompt, schema):
            raise RuntimeError(f"boom ?key={KEY}")

    client = LLMClient(backends=[Exploding()], breaker_threshold=1)
    for attempt in range(2):
        try:
            client.complete("exception_triage", f"no cassette {attempt}", {})
        except LLMUnavailable:
            pass

    assert client.degraded
    assert KEY not in client.degraded_reason
