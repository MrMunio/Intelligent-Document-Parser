"""Runs before the API starts: wait for the database, apply migrations, seed the super admin."""
import logging
import sys
import time
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from shared import runtime_config
from shared.config import settings
from shared.database import SessionLocal, engine
from shared.models import ROLE_SUPER_ADMIN, User
from shared.security import hash_password, password_policy_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s - prestart - %(levelname)s - %(message)s")
log = logging.getLogger("prestart")


def wait_for_database(attempts: int = 60) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            log.info("database not ready (%s/%s): %s", attempt, attempts, str(exc).splitlines()[0])
            time.sleep(2)
    log.error("database did not become ready")
    sys.exit(1)


def migrate() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    logging.getLogger().setLevel(logging.INFO)  # alembic's logging config lowers the root level
    log.info("database schema is up to date")


def seed_super_admin() -> None:
    db = SessionLocal()
    try:
        users = db.query(User).all()
        if any(ROLE_SUPER_ADMIN in (u.roles or []) for u in users):
            return
        email = settings.super_admin_email.strip().lower()
        password = settings.super_admin_password
        if not password:
            log.error("no super admin exists and SUPER_ADMIN_PASSWORD is not set. Set it in .env and restart.")
            sys.exit(1)
        problem = password_policy_error(password)
        if problem:
            log.error("SUPER_ADMIN_PASSWORD rejected: %s", problem)
            sys.exit(1)
        db.add(User(id=str(uuid.uuid4()), email=email, full_name=settings.super_admin_name,
                    password_hash=hash_password(password), roles=[ROLE_SUPER_ADMIN], is_active=True,
                    must_change_password=False))
        db.commit()
        log.info("seeded super admin account %s", email)
    finally:
        db.close()


def seed_engine_settings() -> None:
    """First start: engine settings per service come from .env; afterwards administrators edit them in the web app."""
    seeded = runtime_config.seed_from_env()
    if seeded:
        log.info("engine settings seeded from .env for: %s", ", ".join(seeded))


if __name__ == "__main__":
    if settings.jwt_secret in ("", "change-me-in-env") or len(settings.jwt_secret) < 32:
        log.error("JWT_SECRET must be set to a random value of at least 32 characters.")
        sys.exit(1)
    wait_for_database()
    migrate()
    seed_super_admin()
    seed_engine_settings()
