"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Raw SQL via op.execute so the SQL matches docs/SPEC.md exactly.
Any new tenant table needs ENABLE + FORCE RLS, policies and grants here
(tests/rls/test_rls_catalog.py fails otherwise).
"""

from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    op.execute("""
    """)


def downgrade() -> None:
    op.execute("""
    """)
