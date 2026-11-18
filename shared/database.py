from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from shared.config import settings

def _database_url() -> str:
    # pg8000 (BSD-3, pure Python) is used instead of the LGPL psycopg drivers.
    url = settings.database_url
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+pg8000://" + url[len(prefix):]
    return url


engine = create_engine(
    _database_url(),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
)


@event.listens_for(engine, "connect")
def _set_search_path(dbapi_connection, _record):
    # Every pooled connection resolves unqualified names against idp_schema first.
    cursor = dbapi_connection.cursor()
    cursor.execute("SET search_path TO idp_schema, public")
    cursor.close()
    dbapi_connection.commit()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
