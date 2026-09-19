"""Make ``audit_event`` append-only at the database.

The audit chain is the compliance product. Its credibility rests on the claim
that history cannot be quietly edited, so that claim is enforced here rather
than only in application code that a future caller could bypass.

**Three layers, and what each one actually covers.**

1. *Grants.* The runtime role ``axon_app`` is created here with INSERT and
   SELECT on ``audit_event`` and no UPDATE or DELETE. This is the layer that
   stops the application's own credentials from rewriting history, including
   through an ORM mistake or an injected statement.

   This layer is worth nothing if the application connects as a superuser.
   PostgreSQL skips privilege checks entirely for superusers, so the REVOKE
   below would be silently inert. That is precisely why ``postgres_app_user``
   exists in Settings and why the runtime DSN is a different role from the
   migration DSN.

2. *Trigger.* ``BEFORE UPDATE OR DELETE`` raises unconditionally. Unlike
   grants, a trigger fires for superusers too, so this layer covers the case
   of someone connecting with elevated credentials - including an operator at
   a psql prompt who believes they are fixing a typo.

   Its limit: a superuser can ``SET session_replication_role = replica`` to
   suppress it. That is not a gap this migration can close; it is what layer
   three is for.

3. *Hash chain.* Handled in ``backend/app/audit/chain.py``. Keyed with HMAC
   when ``AXON_AUDIT_HMAC_KEY`` is set, and the key never reaches the database
   role, so an attacker who defeats layers one and two still cannot produce a
   chain that verifies.

``TRUNCATE`` is revoked as well. A trigger on UPDATE and DELETE does not fire
for TRUNCATE, so without the revoke there would be a one-statement hole
straight through both database layers.

Revision ID: 0002
Revises: 0001
Created: 2026-09-19 18:50:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept in step with ``Settings.postgres_app_user``. A migration cannot import
#: Settings without making the schema depend on a developer's environment, so
#: the name is literal here and an integration test asserts the two agree.
APP_ROLE = "axon_app"
APP_PASSWORD = "axon_app_local_dev"

APPEND_ONLY_TABLES = ("audit_event",)


def upgrade() -> None:
    _create_runtime_role()
    _grant_runtime_privileges()
    _install_append_only_guard()


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS axon_reject_mutation()")
    # The role is left in place. Dropping a role fails while it owns anything
    # or holds privileges in another database, and a failed downgrade is worse
    # than a leftover role.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")


def _create_runtime_role() -> None:
    """Create the restricted runtime role if it is not already present.

    ``CREATE ROLE`` has no ``IF NOT EXISTS``, hence the DO block. The password
    is set only when the role is created, so re-running the migration against a
    deployment whose password has been rotated does not reset it to the
    development default.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}';
            END IF;
        END
        $$;
        """
    )


def _grant_runtime_privileges() -> None:
    """Give the runtime role what it needs and nothing more.

    Explicitly *not* granted: CREATE on the schema, so the runtime role cannot
    add a table; and ownership of anything, so it cannot ALTER its way out of
    the restrictions below.
    """
    op.execute(f"GRANT CONNECT ON DATABASE axon TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # Future tables inherit the same baseline, so a later migration does not
    # have to remember a grant to be usable. A future append-only table must
    # still revoke explicitly, exactly as below.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    for table in APPEND_ONLY_TABLES:
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {APP_ROLE}")
        # PUBLIC is revoked too. Privileges granted to PUBLIC apply to every
        # role including this one, so leaving it would reopen what was just
        # closed.
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM PUBLIC")


def _install_append_only_guard() -> None:
    """A trigger that refuses mutation regardless of who is connected.

    ``SQLSTATE 55006`` (object_in_use) rather than a bare ``RAISE EXCEPTION``,
    so callers can distinguish "this row is immutable" from a generic error
    without matching on message text.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION axon_reject_mutation()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                '% on % is forbidden: this table is append-only',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = '55006',
                      HINT = 'Append a correcting event instead of editing history.';
        END;
        $$;
        """
    )
    for table in APPEND_ONLY_TABLES:
        # FOR EACH STATEMENT, not FOR EACH ROW: the statement is rejected
        # outright, so there is no reason to pay per-row overhead, and a
        # DELETE matching zero rows must still be refused rather than quietly
        # succeeding.
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH STATEMENT
            EXECUTE FUNCTION axon_reject_mutation();
            """
        )
