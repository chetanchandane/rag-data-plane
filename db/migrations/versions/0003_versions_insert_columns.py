"""Limit app_api's INSERT on document_versions to the columns a registration needs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16

The WITH CHECK policy already forced status = 'pending_upload' and attempts = 0, but the
API could still set worker-owned columns at insert: lease_until, last_error, parser,
chunker_version, embedding_model, chunk_count, indexed_at. Column-level INSERT closes that.
status gets a default so a registration can omit it (attempts already defaults to 0).
The WITH CHECK stays as a second layer in case the grant is ever widened again.
"""

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_versions ALTER COLUMN status SET DEFAULT 'pending_upload'")
    op.execute("REVOKE INSERT ON document_versions FROM app_api")
    op.execute(
        "GRANT INSERT (doc_id, version, tenant_id, sha256, s3_key) ON document_versions TO app_api"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE INSERT (doc_id, version, tenant_id, sha256, s3_key) "
        "ON document_versions FROM app_api"
    )
    op.execute("GRANT INSERT ON document_versions TO app_api")
    op.execute("ALTER TABLE document_versions ALTER COLUMN status DROP DEFAULT")
