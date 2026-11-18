from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from shared.database import Base

SCHEMA = "idp_schema"

# Roles. A user may hold several. Admin roles implicitly grant every service.
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_DOCUMENT_PARSER = "document_parser"
ROLE_PHI_REDACTOR = "phi_redactor"
ALL_ROLES = [ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_DOCUMENT_PARSER, ROLE_PHI_REDACTOR]
ADMIN_ROLES = {ROLE_SUPER_ADMIN, ROLE_ADMIN}

# Services (what a role or an API key scope unlocks).
SERVICE_PARSE = "parse"
SERVICE_PHI = "phi"
ALL_SERVICES = [SERVICE_PARSE, SERVICE_PHI]
ROLE_SERVICES = {
    ROLE_SUPER_ADMIN: {SERVICE_PARSE, SERVICE_PHI},
    ROLE_ADMIN: {SERVICE_PARSE, SERVICE_PHI},
    ROLE_DOCUMENT_PARSER: {SERVICE_PARSE},
    ROLE_PHI_REDACTOR: {SERVICE_PHI},
}

# Job type -> service required to submit or read it.
JOB_SERVICE = {"PARSE": SERVICE_PARSE, "OCR": SERVICE_PARSE, "PHI": SERVICE_PHI}


def services_for_roles(roles) -> set:
    out = set()
    for r in roles or []:
        out |= ROLE_SERVICES.get(r, set())
    return out


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": SCHEMA}

    id = Column(String(36), primary_key=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    full_name = Column(String(255), nullable=False, default="")
    password_hash = Column(String(255), nullable=False)
    roles = Column(JSONB, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False, default=True)
    must_change_password = Column(Boolean, nullable=False, default=False)
    token_version = Column(Integer, nullable=False, default=0)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = {"schema": SCHEMA}

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    prefix = Column(String(16), nullable=False, index=True)
    key_hash = Column(String(64), nullable=False, unique=True)
    scopes = Column(JSONB, nullable=False, default=list)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class JobRequest(Base):
    __tablename__ = "job_requests"
    __table_args__ = {"schema": SCHEMA}

    id = Column(String(50), primary_key=True, index=True)
    job_type = Column(String(20), nullable=False)  # PARSE | OCR | PHI
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING | PROCESSING | COMPLETED | FAILED
    original_file_name = Column(String(255))
    result_data = Column(JSONB, nullable=True)
    result_markdown = Column(Text, nullable=True)
    result_file_url = Column(String(500), nullable=True)
    error_details = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Added by the platform rework
    user_id = Column(String(36), nullable=True, index=True)
    api_key_id = Column(String(36), nullable=True)
    source = Column(String(10), nullable=True)  # ui | api
    input_format = Column(String(20), nullable=True)
    input_path = Column(String(500), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    options = Column(JSONB, nullable=True)
    progress = Column(JSONB, nullable=True)
    stats = Column(JSONB, nullable=True)
    result_file_name = Column(String(255), nullable=True)
    result_media_type = Column(String(120), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_id = Column(String(36), nullable=True)
    actor_email = Column(String(255), nullable=True)
    action = Column(String(60), nullable=False, index=True)
    target_type = Column(String(40), nullable=True)
    target_id = Column(String(60), nullable=True)
    detail = Column(JSONB, nullable=True)
    ip_address = Column(String(64), nullable=True)
