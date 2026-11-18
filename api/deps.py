"""Authentication and authorization dependencies. All access rules are enforced here, on the server."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from shared.database import get_db
from shared.models import ADMIN_ROLES, ROLE_SUPER_ADMIN, ApiKey, AuditLog, User, services_for_roles
from shared.security import TOKEN_ACCESS, TokenError, decode_token, hash_api_key, looks_like_api_key


@dataclass
class Principal:
    user: User
    api_key: Optional[ApiKey] = None
    services: set = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.api_key is None and bool(ADMIN_ROLES & set(self.user.roles or []))

    @property
    def is_super_admin(self) -> bool:
        return self.api_key is None and ROLE_SUPER_ADMIN in (self.user.roles or [])

    @property
    def via(self) -> str:
        return "api" if self.api_key is not None else "ui"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _principal_from_api_key(raw_key: str, db: Session) -> Principal:
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_api_key(raw_key)).first()
    now = datetime.now(timezone.utc)
    if key is None or key.revoked_at is not None:
        raise _unauthorized("API key is invalid or has been revoked.")
    if key.expires_at is not None and key.expires_at < now:
        raise _unauthorized("API key has expired.")
    user = db.query(User).filter(User.id == key.user_id).first()
    if user is None or not user.is_active:
        raise _unauthorized("The account that owns this API key is disabled.")
    # Record usage at most once a minute to avoid a write on every request.
    if key.last_used_at is None or (now - key.last_used_at).total_seconds() > 60:
        key.last_used_at = now
        db.commit()
    # A key can never do more than its owner can do right now.
    services = set(key.scopes or []) & services_for_roles(user.roles)
    return Principal(user=user, api_key=key, services=services)


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    raw_key = request.headers.get("x-api-key")
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    if not raw_key and bearer and looks_like_api_key(bearer):
        raw_key = bearer
    if raw_key:
        return _principal_from_api_key(raw_key.strip(), db)

    if not bearer:
        raise _unauthorized("Authentication required. Send a Bearer access token or an X-API-Key header.")
    try:
        payload = decode_token(bearer, TOKEN_ACCESS)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if user is None or not user.is_active:
        raise _unauthorized("Account is disabled or no longer exists.")
    if payload.get("ver") != user.token_version:
        raise _unauthorized("Session is no longer valid. Please sign in again.")
    return Principal(user=user, services=services_for_roles(user.roles))


def require_service(service: str):
    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.api_key is None and principal.user.must_change_password:
            raise HTTPException(status_code=403, detail="Password change required before using this service.")
        if service not in principal.services:
            raise HTTPException(status_code=403, detail=f"Your account does not have access to the '{service}' service.")
        return principal
    return _dep


def require_user_session(principal: Principal = Depends(get_principal)) -> Principal:
    """Interactive session only. API keys cannot manage accounts or keys."""
    if principal.api_key is not None:
        raise HTTPException(status_code=403, detail="This endpoint requires a signed-in user session, not an API key.")
    return principal


def require_admin(principal: Principal = Depends(require_user_session)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if principal.user.must_change_password:
        raise HTTPException(status_code=403, detail="Password change required before using this service.")
    return principal


def audit(db: Session, action: str, *, actor: Optional[User] = None, actor_email: Optional[str] = None,
          target_type: Optional[str] = None, target_id: Optional[str] = None, detail: Optional[dict] = None,
          request: Optional[Request] = None, commit: bool = True) -> None:
    db.add(AuditLog(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else actor_email,
        action=action, target_type=target_type, target_id=target_id, detail=detail,
        ip_address=client_ip(request) if request else None,
    ))
    if commit:
        db.commit()
