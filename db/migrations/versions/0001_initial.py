"""Initial schema: tables, indexes, RLS helper functions, policies and grants.

Revision ID: 0001
Revises:
Create Date: 2026-11-16

Matches docs/SPEC.md "Data model and RLS". Runs as app_owner.
Prerequisites created by db/bootstrap.py (not here, because migrations get rolled back and
roles/passwords must not be): the roles app_owner, app_api, app_ingest and the vector extension.

Each statement is its own op.execute call: the psycopg driver will not run several
statements in one call, and a failure then points at the exact statement.
"""

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


TABLES = [
    """
    CREATE TABLE documents (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL,
      external_id     text NOT NULL,
      title           text,
      min_role_level  smallint NOT NULL CHECK (min_role_level BETWEEN 1 AND 3),
      active_version  int,
      deleted_at      timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, external_id),
      UNIQUE (id, tenant_id)   -- target of the tenant-consistent FK from document_versions
    )
    """,
    # FK checks bypass RLS, so a plain FK on doc_id alone would let a row point at another
    # tenant's document. Including tenant_id in the FK makes that impossible.
    """
    CREATE TABLE document_versions (
      doc_id           uuid NOT NULL,
      version          int  NOT NULL,
      tenant_id        uuid NOT NULL,
      sha256           char(64) NOT NULL,
      s3_key           text NOT NULL,
      status           text NOT NULL CHECK (status IN
                       ('pending_upload','uploaded','parsing','embedding','indexed','failed','superseded')),
      attempts         int  NOT NULL DEFAULT 0,
      lease_until      timestamptz,
      last_error       text,
      parser           text,
      chunker_version  text,
      embedding_model  text,
      chunk_count      int,
      created_at       timestamptz NOT NULL DEFAULT now(),
      indexed_at       timestamptz,
      PRIMARY KEY (doc_id, version),
      UNIQUE (doc_id, sha256),
      UNIQUE (doc_id, version, tenant_id),   -- target of the tenant-consistent FK from chunks
      FOREIGN KEY (doc_id, tenant_id) REFERENCES documents (id, tenant_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE chunks (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL,
      doc_id          uuid NOT NULL,
      version         int  NOT NULL,
      chunk_index     int  NOT NULL,
      min_role_level  smallint NOT NULL,   -- copied from documents at ingest
      page_start      int,
      page_end        int,
      section_path    text,
      content         text NOT NULL,
      embedding       vector(1024) NOT NULL,
      fts             tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
      UNIQUE (doc_id, version, chunk_index),
      FOREIGN KEY (doc_id, version, tenant_id)
        REFERENCES document_versions (doc_id, version, tenant_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE tenant_index_state (   -- bumped on every activation; part of the cache key
      tenant_id uuid PRIMARY KEY,
      epoch     bigint NOT NULL DEFAULT 0
    )
    """,
    # role_level is a column (not only part of cache_key) so RLS can enforce it.
    """
    CREATE UNLOGGED TABLE query_cache (
      cache_key  char(64) PRIMARY KEY,
      tenant_id  uuid NOT NULL,
      role_level smallint NOT NULL,
      response   jsonb NOT NULL,
      expires_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE feedback (
      id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id  uuid NOT NULL,
      trace_id   text NOT NULL,
      helpful    boolean NOT NULL,
      comment    text,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

INDEXES = [
    "CREATE INDEX chunks_acl ON chunks (tenant_id, min_role_level)",
    "CREATE INDEX chunks_fts ON chunks USING gin (fts)",
    # No ANN index in v1: exact search is fast below ~50k chunks and never loses recall to filters.
]

# Fail closed: an unset or reset setting becomes NULL / 0, which matches no rows.
# NULLIF(..., '') matters: after a transaction ends a custom setting reads as '', and
# ''::uuid would raise instead of matching nothing.
FUNCTIONS = [
    """
    CREATE FUNCTION app_tenant() RETURNS uuid LANGUAGE sql STABLE AS
      $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
    """,
    """
    CREATE FUNCTION app_role_level() RETURNS int LANGUAGE sql STABLE AS
      $$ SELECT COALESCE(NULLIF(current_setting('app.role_level', true), '')::int, 0) $$
    """,
    # Functions are executable by PUBLIC by default. Only the app roles need them
    # (policies are evaluated as the querying role).
    "REVOKE EXECUTE ON FUNCTION app_tenant(), app_role_level() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION app_tenant(), app_role_level() TO app_api, app_ingest",
]

TENANT_TABLES = [
    "documents",
    "document_versions",
    "chunks",
    "tenant_index_state",
    "query_cache",
    "feedback",
]

# ENABLE turns RLS on; FORCE applies it to the table owner (app_owner) too, which then
# sees zero rows because no policy names it.
# The f-string only inserts table names from the constant list above, never input values.
ENABLE_RLS = [
    stmt
    for table in TENANT_TABLES
    for stmt in (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
    )
]

POLICIES = [
    # documents. app_api is limited to its role level for reads and writes, so a role-1 user
    # cannot create or edit a role-3 document. Which columns it may UPDATE is set by grants.
    """
    CREATE POLICY documents_api ON documents FOR ALL TO app_api
      USING      (tenant_id = app_tenant() AND min_role_level <= app_role_level())
      WITH CHECK (tenant_id = app_tenant() AND min_role_level <= app_role_level())
    """,
    """
    CREATE POLICY documents_ingest ON documents FOR ALL TO app_ingest
      USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())
    """,
    # document_versions has no min_role_level. The EXISTS subquery runs under the caller's
    # RLS on documents, so a version is visible only if its document is.
    """
    CREATE POLICY document_versions_api_read ON document_versions FOR SELECT TO app_api
      USING (tenant_id = app_tenant()
             AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_versions.doc_id))
    """,
    # The API may only register new versions. Status changes belong to the worker.
    """
    CREATE POLICY document_versions_api_insert ON document_versions FOR INSERT TO app_api
      WITH CHECK (tenant_id = app_tenant()
                  AND status = 'pending_upload' AND attempts = 0
                  AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_versions.doc_id))
    """,
    """
    CREATE POLICY document_versions_ingest ON document_versions FOR ALL TO app_ingest
      USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())
    """,
    """
    CREATE POLICY chunks_api_read ON chunks FOR SELECT TO app_api
      USING (tenant_id = app_tenant() AND min_role_level <= app_role_level())
    """,
    """
    CREATE POLICY chunks_ingest_rw ON chunks FOR ALL TO app_ingest
      USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())
    """,
    """
    CREATE POLICY tenant_index_state_rw ON tenant_index_state FOR ALL TO app_api, app_ingest
      USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())
    """,
    # Isolation by tenant AND role level in the policy itself, not only via the cache key.
    """
    CREATE POLICY query_cache_api ON query_cache FOR ALL TO app_api
      USING      (tenant_id = app_tenant() AND role_level = app_role_level())
      WITH CHECK (tenant_id = app_tenant() AND role_level = app_role_level())
    """,
    """
    CREATE POLICY feedback_api_insert ON feedback FOR INSERT TO app_api
      WITH CHECK (tenant_id = app_tenant())
    """,
]

GRANTS = [
    # Belt and braces: nothing in this schema is readable by PUBLIC.
    "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC",
    # app_api. Column-level UPDATE on documents: the API must not change min_role_level
    # (chunks would go out of sync) or active_version (activation is the worker's job).
    "GRANT SELECT, INSERT ON documents TO app_api",
    "GRANT UPDATE (title, deleted_at) ON documents TO app_api",
    "GRANT SELECT, INSERT ON document_versions TO app_api",
    "GRANT SELECT ON chunks TO app_api",
    "GRANT SELECT, INSERT, UPDATE ON tenant_index_state TO app_api",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON query_cache TO app_api",
    "GRANT INSERT ON feedback TO app_api",
    # app_ingest
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
      ON documents, document_versions, chunks, tenant_index_state TO app_ingest
    """,
]


def upgrade() -> None:
    for stmt in TABLES + INDEXES + FUNCTIONS + ENABLE_RLS + POLICIES + GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    # Policies, indexes and grants are dropped with their tables. Roles and the vector
    # extension belong to db/bootstrap.py and stay.
    op.execute(
        "DROP TABLE feedback, query_cache, tenant_index_state, chunks, document_versions, documents"
    )
    op.execute("DROP FUNCTION app_role_level(), app_tenant()")
