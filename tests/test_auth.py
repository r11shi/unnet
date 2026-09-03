"""Who may change financial state.

Reading is public — the deployed demo is meant to be walked by anyone. Writing
is not: settling a case and writing to the audit trail are financial actions,
and an unauthenticated mutation endpoint on a finance system is the first thing
a reviewer looks for.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A fresh app against an empty database, so tests never touch real data."""
    monkeypatch.setenv("UNNET_DB", str(tmp_path / "auth.db"))
    monkeypatch.delenv("UNNET_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("UNNET_ENV", raising=False)
    import unnet.api.main as main

    importlib.reload(main)
    return TestClient(main.app), main


def test_reads_never_require_a_token(client, monkeypatch):
    api, _ = client
    monkeypatch.setenv("UNNET_ADMIN_TOKEN", "secret")
    assert api.get("/api/health").status_code == 200
    # A read on an empty database is a 404 about missing data, never a 401.
    assert api.get("/api/cases").status_code != 401


def test_local_with_no_token_allows_writes(client):
    """`make demo` and the tests must work with no setup."""
    from unnet.api.auth import require_write

    assert require_write(None) == "local"


def test_production_with_no_token_refuses_writes(client, monkeypatch):
    """Shipping a deployment whose secret was never configured should fail
    closed, not quietly serve an open endpoint."""
    from fastapi import HTTPException

    from unnet.api.auth import require_write, writes_enabled

    monkeypatch.setenv("UNNET_ENV", "production")
    assert not writes_enabled()
    with pytest.raises(HTTPException) as caught:
        require_write(None)
    assert caught.value.status_code == 503
    assert "read-only" in caught.value.detail


def test_a_configured_token_is_required_and_must_match(client, monkeypatch):
    from fastapi import HTTPException

    from unnet.api.auth import require_write

    monkeypatch.setenv("UNNET_ADMIN_TOKEN", "correct-horse")

    for bad in (None, "Bearer wrong", "correct-horse", "Basic correct-horse"):
        with pytest.raises(HTTPException) as caught:
            require_write(bad)
        assert caught.value.status_code == 401, bad

    assert require_write("Bearer correct-horse") == "operator"


def test_the_resolve_endpoint_is_actually_guarded(client, monkeypatch):
    """The dependency must be wired to the route, not merely exist."""
    api, _ = client
    monkeypatch.setenv("UNNET_ADMIN_TOKEN", "s3cret")

    unauthorised = api.post("/api/cases/anything/resolve", json={"note": "x"})
    assert unauthorised.status_code == 401

    # With the token it gets past auth and fails on the missing case instead.
    authorised = api.post(
        "/api/cases/anything/resolve",
        json={"note": "x"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert authorised.status_code == 404


def test_readiness_fails_when_there_is_no_run_to_serve(client):
    """A process that is up but has nothing to show should not take traffic."""
    api, _ = client
    response = api.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["database"] is True
    assert response.json()["detail"]["run_present"] is False


# --------------------------------------------------------------------------- #
# Whether a write survives a restart is a property of the deployment, and an
# operator settling cases is entitled to know it before they start.
# --------------------------------------------------------------------------- #


def test_storage_is_assumed_durable_unless_a_deployment_says_otherwise(monkeypatch):
    from unnet.api.auth import storage_is_durable

    monkeypatch.delenv("UNNET_STORAGE", raising=False)
    assert storage_is_durable() is True


def test_a_deployment_with_no_disk_can_declare_itself_ephemeral(monkeypatch):
    """Render's free tier attaches none, so every settled case reverts on
    restart. A finance tool that loses writes silently is worse than one that
    says so."""
    from unnet.api.auth import storage_is_durable

    monkeypatch.setenv("UNNET_STORAGE", "ephemeral")
    assert storage_is_durable() is False
    monkeypatch.setenv("UNNET_STORAGE", "  EPHEMERAL  ")
    assert storage_is_durable() is False


def test_readiness_reports_storage_durability(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from unnet.api.main import app

    monkeypatch.setenv("UNNET_STORAGE", "ephemeral")
    body = TestClient(app, raise_server_exceptions=False).get("/api/ready").json()
    payload = body.get("detail", body)
    assert payload["storage_durable"] is False
