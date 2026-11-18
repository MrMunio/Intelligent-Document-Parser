"""Platform settings: engine configuration per service, editable by administrators.

Rows are seeded from .env when the API first starts; administrators change them in the web app.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

S = "idp_schema"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE IF NOT EXISTS {S}.platform_settings (
        key VARCHAR(60) PRIMARY KEY,
        value JSONB NOT NULL,
        source VARCHAR(20) NOT NULL DEFAULT 'env',
        updated_by VARCHAR(255),
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )""")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {S}.platform_settings")
