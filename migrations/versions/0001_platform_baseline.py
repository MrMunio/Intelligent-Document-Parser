"""Platform baseline: users, API keys, audit log and the extended job table.

Safe on both a fresh database and one that already holds the legacy job_requests table.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

S = "idp_schema"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE IF NOT EXISTS {S}.users (
        id VARCHAR(36) PRIMARY KEY,
        email VARCHAR(255) NOT NULL UNIQUE,
        full_name VARCHAR(255) NOT NULL DEFAULT '',
        password_hash VARCHAR(255) NOT NULL,
        roles JSONB NOT NULL DEFAULT '[]'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
        token_version INTEGER NOT NULL DEFAULT 0,
        last_login_at TIMESTAMPTZ,
        created_by VARCHAR(36),
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )""")

    op.execute(f"""
    CREATE TABLE IF NOT EXISTS {S}.api_keys (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES {S}.users(id) ON DELETE CASCADE,
        name VARCHAR(120) NOT NULL,
        prefix VARCHAR(16) NOT NULL,
        key_hash VARCHAR(64) NOT NULL UNIQUE,
        scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
        expires_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        last_used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON {S}.api_keys (user_id)")

    op.execute(f"""
    CREATE TABLE IF NOT EXISTS {S}.audit_logs (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        actor_id VARCHAR(36),
        actor_email VARCHAR(255),
        action VARCHAR(60) NOT NULL,
        target_type VARCHAR(40),
        target_id VARCHAR(60),
        detail JSONB,
        ip_address VARCHAR(64)
    )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON {S}.audit_logs (created_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON {S}.audit_logs (action)")

    op.execute(f"""
    CREATE TABLE IF NOT EXISTS {S}.job_requests (
        id VARCHAR(50) PRIMARY KEY,
        job_type VARCHAR(20) NOT NULL,
        status VARCHAR(20) NOT NULL,
        original_file_name VARCHAR(255),
        result_data JSONB,
        result_markdown TEXT,
        result_file_url VARCHAR(500),
        error_details TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )""")
    for column in (
        "user_id VARCHAR(36)", "api_key_id VARCHAR(36)", "source VARCHAR(10)", "input_format VARCHAR(20)",
        "input_path VARCHAR(500)", "file_size BIGINT", "options JSONB", "progress JSONB", "stats JSONB",
        "result_file_name VARCHAR(255)", "result_media_type VARCHAR(120)", "started_at TIMESTAMPTZ",
        "completed_at TIMESTAMPTZ", "duration_ms INTEGER",
    ):
        op.execute(f"ALTER TABLE {S}.job_requests ADD COLUMN IF NOT EXISTS {column}")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_job_requests_user_id ON {S}.job_requests (user_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_job_requests_created_at ON {S}.job_requests (created_at DESC)")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {S}.api_keys")
    op.execute(f"DROP TABLE IF EXISTS {S}.audit_logs")
    op.execute(f"DROP TABLE IF EXISTS {S}.users")
