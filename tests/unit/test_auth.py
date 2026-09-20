"""A principal comes from a verified signature, never from a request.

Threat T5. Everything the policy matrix guarantees rests on the role in a
`Principal` having been proved rather than asserted, so these tests are about
what an attacker who controls the token cannot achieve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from backend.app.auth.tokens import (
    SIGNING_ALGORITHM,
    InvalidTokenError,
    TokenExpiredError,
    issue_token,
    principal_from_token,
)
from backend.app.config import Settings
from backend.app.policies.engine import Role


#: Tokens are verified against the real clock - PyJWT offers no way to inject
#: one - so a helper that minted against a fixed instant would hand every test
#: an already-expired token as soon as that instant passed. Only the cases
#: that *want* expiry pin a time, and they pin one relative to this.
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def settings() -> Settings:
    """Explicit settings, so the tests do not depend on a developer's `.env`."""
    return Settings(
        _env_file=None,
        axon_jwt_secret="test-signing-secret-long-enough-to-be-plausible",
        axon_jwt_issuer="axonfde-test",
        axon_jwt_audience="axonfde-api-test",
    )


def token(settings: Settings, **overrides: object) -> str:
    """Mint a token whose claims can be tampered with one at a time."""
    claims: dict[str, object] = {
        "sub": "dispatcher@axon.test",
        "role": Role.DISPATCHER.value,
        "iss": settings.axon_jwt_issuer,
        "aud": settings.axon_jwt_audience,
        "iat": int(now().timestamp()),
        "exp": int((now() + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(
        claims, settings.axon_jwt_secret.get_secret_value(), algorithm=SIGNING_ALGORITHM
    )


class TestAValidToken:
    def test_the_role_comes_out_of_the_token(self, settings: Settings) -> None:
        minted = issue_token("fleet@axon.test", Role.FLEET_MANAGER, settings=settings)
        principal = principal_from_token(minted, settings=settings)
        assert principal.subject == "fleet@axon.test"
        assert principal.role is Role.FLEET_MANAGER

    def test_a_principal_is_the_only_thing_the_policy_engine_is_given(
        self, settings: Settings
    ) -> None:
        """There is no path from a request body to a role.

        Stated as a test because it is a claim about the shape of the system
        rather than about a branch: `principal_from_token` takes a token and
        settings, and nothing else. A role supplied anywhere else has nowhere
        to enter.
        """
        import inspect

        parameters = set(inspect.signature(principal_from_token).parameters)
        assert parameters == {"token", "settings"}


class TestWhatAnAttackerCannotDo:
    def test_a_token_claiming_admin_but_signed_with_the_wrong_key_is_refused(
        self, settings: Settings
    ) -> None:
        """Self-service privilege escalation, the obvious attempt."""
        forged = jwt.encode(
            {
                "sub": "attacker@evil.test",
                "role": Role.ADMIN.value,
                "iss": settings.axon_jwt_issuer,
                "aud": settings.axon_jwt_audience,
                "iat": int(now().timestamp()),
                "exp": int((now() + timedelta(hours=1)).timestamp()),
            },
            "not-the-signing-secret-but-long-enough-to-avoid-a-warning",
            algorithm=SIGNING_ALGORITHM,
        )
        with pytest.raises(InvalidTokenError):
            principal_from_token(forged, settings=settings)

    def test_an_unsigned_token_is_refused(self, settings: Settings) -> None:
        """`alg: none`.

        The attack works when a verifier honours the algorithm the *token*
        names. The explicit allowlist in `principal_from_token` is the one
        line that closes it, and this test is what stops somebody removing
        that argument because "PyJWT figures it out".
        """
        unsigned = jwt.encode(
            {
                "sub": "attacker@evil.test",
                "role": Role.ADMIN.value,
                "iss": settings.axon_jwt_issuer,
                "aud": settings.axon_jwt_audience,
                "iat": int(now().timestamp()),
                "exp": int((now() + timedelta(hours=1)).timestamp()),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(InvalidTokenError):
            principal_from_token(unsigned, settings=settings)

    def test_a_token_minted_for_another_service_does_not_authenticate_here(
        self, settings: Settings
    ) -> None:
        """A correct signature over the wrong audience is still the wrong token.

        Same issuer, same key, different service. Without audience
        verification this authenticates, which is how a low-value system's
        credentials become a high-value system's.
        """
        with pytest.raises(InvalidTokenError):
            principal_from_token(token(settings, aud="some-other-service"), settings=settings)

    def test_a_token_from_an_unrecognised_issuer_is_refused(self, settings: Settings) -> None:
        with pytest.raises(InvalidTokenError):
            principal_from_token(token(settings, iss="https://evil.test"), settings=settings)

    def test_an_unrecognised_role_is_refused_rather_than_demoted(self, settings: Settings) -> None:
        """Not silently downgraded to viewer.

        Demoting would make a typo in an issuer's configuration read as a
        working login with mysteriously missing permissions, and the audit
        trail would then record a role that was never granted.
        """
        with pytest.raises(InvalidTokenError, match="not a role this service recognises"):
            principal_from_token(token(settings, role="superuser"), settings=settings)

    def test_a_token_with_no_role_claim_is_refused(self, settings: Settings) -> None:
        claims = {
            "sub": "nobody@axon.test",
            "iss": settings.axon_jwt_issuer,
            "aud": settings.axon_jwt_audience,
            "iat": int(now().timestamp()),
            "exp": int((now() + timedelta(hours=1)).timestamp()),
        }
        bare = jwt.encode(
            claims, settings.axon_jwt_secret.get_secret_value(), algorithm=SIGNING_ALGORITHM
        )
        with pytest.raises(InvalidTokenError, match="no role claim"):
            principal_from_token(bare, settings=settings)

    def test_a_token_with_an_empty_subject_is_refused(self, settings: Settings) -> None:
        """An audit trail needs somebody's name in the actor column."""
        with pytest.raises(InvalidTokenError, match="no usable subject"):
            principal_from_token(token(settings, sub=""), settings=settings)


class TestExpiry:
    def test_an_expired_token_reports_expiry_specifically(self, settings: Settings) -> None:
        """Distinct from a bad signature, for operations rather than for the client.

        Counting expiries and forgeries together hides a credential-stuffing
        attempt inside a wall of ordinary refreshes.
        """
        stale = issue_token(
            "dispatcher@axon.test",
            Role.DISPATCHER,
            settings=settings,
            issued_at=now() - timedelta(hours=2),
            ttl=timedelta(minutes=5),
        )
        with pytest.raises(TokenExpiredError):
            principal_from_token(stale, settings=settings)

    def test_a_token_with_no_expiry_is_refused(self, settings: Settings) -> None:
        """A token that never expires is a standing grant.

        `exp` is in the required-claims list precisely so that omitting it is
        a rejection rather than an unlimited session.
        """
        claims = {
            "sub": "forever@axon.test",
            "role": Role.ADMIN.value,
            "iss": settings.axon_jwt_issuer,
            "aud": settings.axon_jwt_audience,
            "iat": int(now().timestamp()),
        }
        eternal = jwt.encode(
            claims, settings.axon_jwt_secret.get_secret_value(), algorithm=SIGNING_ALGORITHM
        )
        with pytest.raises(InvalidTokenError):
            principal_from_token(eternal, settings=settings)


def test_issuing_a_token_without_a_subject_is_refused(settings: Settings) -> None:
    with pytest.raises(ValueError, match="must have a subject"):
        issue_token("", Role.VIEWER, settings=settings)
