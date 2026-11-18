from logging.config import fileConfig

from alembic import context
from sqlalchemy import text

from shared.database import Base, engine
from shared import models  # noqa: F401  (registers the tables on Base.metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
SCHEMA = "idp_schema"


def run_migrations_online() -> None:
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata,
                          version_table_schema=SCHEMA, include_schemas=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
