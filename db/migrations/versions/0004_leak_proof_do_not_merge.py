"""DO NOT MERGE. A deliberately broken policy, to prove the leak matrix fails CI (step 17's
"done when"; DECISIONS 2026-11-22, "Step 17"). Close the pull request unmerged.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22

The bug: app_api's read policies on chunks and documents keep the tenant check but lose the
role-level check, so a role-1 user sees role-2 and role-3 documents. Both policies, because
the hybrid SQL joins chunks to documents: with only the chunks policy broken, the documents
policy in the join still hides the rows.
"""

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("DROP POLICY chunks_api_read ON chunks")
    op.execute(
        "CREATE POLICY chunks_api_read ON chunks FOR SELECT TO app_api"
        " USING (tenant_id = app_tenant())"
    )
    op.execute("DROP POLICY documents_api ON documents")
    op.execute(
        "CREATE POLICY documents_api ON documents FOR ALL TO app_api"
        " USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY chunks_api_read ON chunks")
    op.execute(
        "CREATE POLICY chunks_api_read ON chunks FOR SELECT TO app_api"
        " USING (tenant_id = app_tenant() AND min_role_level <= app_role_level())"
    )
    op.execute("DROP POLICY documents_api ON documents")
    op.execute(
        "CREATE POLICY documents_api ON documents FOR ALL TO app_api"
        " USING (tenant_id = app_tenant() AND min_role_level <= app_role_level())"
        " WITH CHECK (tenant_id = app_tenant() AND min_role_level <= app_role_level())"
    )
