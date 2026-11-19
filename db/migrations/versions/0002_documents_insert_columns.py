"""Limit app_api's INSERT on documents to the columns a registration needs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16

0001 granted table-level INSERT, so the API could create a document with active_version
already set (blocking forward-only activation, so real versions end up superseded),
deleted_at set, or a caller-chosen id / created_at. Column-level INSERT closes that;
id and created_at still get their defaults (defaults need no column privilege).
"""

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("REVOKE INSERT ON documents FROM app_api")
    op.execute(
        "GRANT INSERT (tenant_id, external_id, title, min_role_level) ON documents TO app_api"
    )


def downgrade() -> None:
    # Column grants and table grants are separate entries, so revoke the column one explicitly.
    op.execute(
        "REVOKE INSERT (tenant_id, external_id, title, min_role_level) ON documents FROM app_api"
    )
    op.execute("GRANT INSERT ON documents TO app_api")
