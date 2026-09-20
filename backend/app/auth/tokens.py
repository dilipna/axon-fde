"""Turning a bearer token into a ``Principal``.

**This module exists to make one thing impossible: a caller choosing its own
role.** That is threat T5. The policy engine takes a ``Principal`` and trusts
it, so everything the policy matrix guarantees rests on the principal having
been derived from a verified signature rather than from anything the client
sent in a body, a query string or a header it controls.

Three details here are security-relevant rather than stylistic:

- ``algorithms`` is an explicit allowlist. PyJWT will otherwise honour the
  token's own ``alg`` header, which is how ``alg: none`` and the
  HMAC-vs-RSA confusion attacks work: the attacker picks the algorithm and
  the verifier obliges. The allowlist means an attacker-chosen algorithm is
  rejected before the signature is even considered.
- ``aud`` and ``iss`` are *required* and verified. A token minted for a
  different service by the same issuer is a valid signature over the wrong
  claims, and without audience checking it authenticates here too.
- An unrecognised ``role`` claim is a rejection, not a downgrade to viewer.
  Silently demoting would mean a typo in an issuer's configuration reads as
  a working login with mysteriously missing permissions, and the audit trail
  would record "viewer" where no valid role existed at all.

The module is deliberately small and I/O-free: no key fetches, no user
lookups. Its only input is the token and the configured secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from backend.app.config import Settings, get_settings
from backend.app.policies.engine import Principal, Role

__all__ = [
    "SIGNING_ALGORITHM",
    "InvalidTokenError",
    "TokenError",
    "TokenExpiredError",
    "issue_token",
    "principal_from_token",
]

#: The one algorithm this service accepts. Symmetric because the issuer and
#: the verifier are the same deployment in Phase 1; moving to RS256 means
#: changing this constant and the key material, not the verification logic.
SIGNING_ALGORITHM = "HS256"


class TokenError(Exception):
    """A token was not accepted.

    One base class because the *caller* must not branch on why: telling an
    unauthenticated client whether its signature was wrong or merely its
    audience is a small oracle. The subclasses exist for logging and for
    tests, which are inside the trust boundary.
    """


class TokenExpiredError(TokenError):
    """The token was well formed and correctly signed, but past its life.

    Separate from ``InvalidTokenError`` because the operational response
    differs: an expired token means refresh, a bad signature means something
    is wrong. Counting them together would hide a credential-stuffing attempt
    inside a wall of ordinary expiries.
    """


class InvalidTokenError(TokenError):
    """The token failed verification, or carried claims we cannot honour."""


def issue_token(
    subject: str,
    role: Role,
    *,
    settings: Settings | None = None,
    issued_at: datetime | None = None,
    ttl: timedelta | None = None,
) -> str:
    """Mint a token for a subject and role.

    Present so tests and the demo can authenticate without standing up an
    identity provider. In a real deployment the issuer is external and this
    function is not on the request path — which is why it takes the role as an
    argument and does no authorisation of its own. It mints what it is told
    to; deciding *who gets which role* is a different system's job.
    """
    config = settings or get_settings()
    if not subject:
        raise ValueError("a token must have a subject")

    now = issued_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")
    lifetime = ttl or timedelta(seconds=config.axon_jwt_ttl_seconds)

    claims: dict[str, Any] = {
        "sub": subject,
        "role": role.value,
        "iss": config.axon_jwt_issuer,
        "aud": config.axon_jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(
        claims, config.axon_jwt_secret.get_secret_value(), algorithm=SIGNING_ALGORITHM
    )


def principal_from_token(token: str, *, settings: Settings | None = None) -> Principal:
    """Verify a token and return who it says is asking.

    Raises:
        TokenExpiredError: The signature was good but the token has expired.
        InvalidTokenError: Anything else — bad signature, wrong audience or
            issuer, missing or unrecognised claims.
    """
    config = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            config.axon_jwt_secret.get_secret_value(),
            # An allowlist, not the token's own preference. See the module
            # docstring: this single argument is what closes `alg: none`.
            algorithms=[SIGNING_ALGORITHM],
            audience=config.axon_jwt_audience,
            issuer=config.axon_jwt_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("the token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, a rejected algorithm, a wrong audience or
        # issuer, and a missing required claim. Deliberately one message.
        raise InvalidTokenError("the token could not be verified") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidTokenError("the token carries no usable subject")

    raw_role = claims.get("role")
    if not isinstance(raw_role, str):
        raise InvalidTokenError("the token carries no role claim")
    try:
        role = Role(raw_role)
    except ValueError as exc:
        # Refused rather than demoted. A role this service does not recognise
        # is a configuration fault at the issuer, and guessing at it would put
        # an invented role in the audit trail.
        raise InvalidTokenError(f"{raw_role!r} is not a role this service recognises") from exc

    return Principal(subject=subject, role=role)
