"""Who may change financial state.

Reading is public: the point of the deployed demo is that anyone can open it,
walk a case, and see the agent trace. Writing is not, because settling a case
and writing to the audit trail are financial actions, and an unauthenticated
mutation endpoint on a finance system is the kind of thing a reviewer notices
before anything else.

Deliberately not a user system. One bearer token from the environment covers the
whole surface, which is the right amount of machinery for a single-operator
prototype and can be replaced by real identity without touching call sites.

The default matters more than the mechanism:

* No token set, running locally — writes allowed, so `make demo` and the tests
  work with no setup.
* No token set, ``UNNET_ENV=production`` — writes **refused**. Shipping a
  deployment whose secret was never configured should fail closed, not quietly
  serve an open endpoint.
* Token set — writes require it, everywhere.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def admin_token() -> str:
    return os.environ.get("UNNET_ADMIN_TOKEN", "").strip()


def is_production() -> bool:
    return os.environ.get("UNNET_ENV", "").lower() in {"production", "prod"}


def writes_enabled() -> bool:
    """Whether this deployment can accept a write at all."""
    return bool(admin_token()) or not is_production()


def require_write(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency guarding every endpoint that changes state.

    Returns the identity to attribute the action to, so the audit trail records
    *who* settled a case rather than just that it happened.
    """
    token = admin_token()

    if not token:
        if is_production():
            raise HTTPException(
                503,
                "This deployment is read-only: UNNET_ADMIN_TOKEN is not configured. "
                "Set it to enable case actions.",
            )
        return "local"

    supplied = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()

    # Constant-time compare: a token check that leaks its answer through timing
    # is not a token check.
    if not supplied or not hmac.compare_digest(supplied, token):
        raise HTTPException(
            401,
            "A valid bearer token is required for case actions.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "operator"


def storage_is_durable() -> bool:
    """Whether a write made here survives the instance restarting.

    Render's free tier attaches no disk, so `data/unnet.db` lives in the
    container layer: the service spins down after inactivity and every settled
    case reverts. "A settled case stays settled" is this system's central
    claim, and on that deployment it holds only until the dyno sleeps.

    A finance tool that loses writes silently is worse than one that says so,
    which is the whole reason this is a declared fact rather than a guess.
    Default is durable — a local run on a real filesystem is — and a deployment
    without a disk sets UNNET_STORAGE=ephemeral.
    """
    return os.environ.get("UNNET_STORAGE", "durable").strip().lower() != "ephemeral"
