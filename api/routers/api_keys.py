import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from api.deps import Principal, audit, require_user_session
from api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from shared.database import get_db
from shared.models import ALL_SERVICES, ApiKey, User, services_for_roles
from shared.security import generate_api_key

router = APIRouter(prefix="/api-keys", tags=["API keys"])


def _out(key: ApiKey, owner_email: str | None = None) -> ApiKeyOut:
    return ApiKeyOut(id=key.id, name=key.name, prefix=key.prefix, scopes=list(key.scopes or []),
                     created_at=key.created_at, expires_at=key.expires_at, last_used_at=key.last_used_at,
                     revoked_at=key.revoked_at, owner_email=owner_email)


@router.get("", response_model=list[ApiKeyOut], summary="List API keys")
def list_keys(all_users: bool = Query(False, description="Administrators only: include keys of every user"),
              principal: Principal = Depends(require_user_session), db: Session = Depends(get_db)):
    query = db.query(ApiKey, User.email).join(User, User.id == ApiKey.user_id)
    if not (all_users and principal.is_admin):
        query = query.filter(ApiKey.user_id == principal.user.id)
    rows = query.order_by(ApiKey.created_at.desc()).all()
    return [_out(key, email) for key, email in rows]


@router.post("", response_model=ApiKeyCreated, status_code=201,
             summary="Create an API key for service-to-service integration")
def create_key(body: ApiKeyCreate, request: Request, principal: Principal = Depends(require_user_session),
               db: Session = Depends(get_db)):
    scopes = sorted(set(body.scopes))
    if any(s not in ALL_SERVICES for s in scopes):
        raise HTTPException(status_code=400, detail=f"Scopes must be chosen from: {', '.join(ALL_SERVICES)}.")
    allowed = services_for_roles(principal.user.roles)
    if not set(scopes) <= allowed:
        raise HTTPException(status_code=403, detail="You can only grant scopes for services your account can use.")
    if principal.user.must_change_password:
        raise HTTPException(status_code=403, detail="Password change required before creating API keys.")

    plaintext, prefix, key_hash = generate_api_key()
    expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    key = ApiKey(id=str(uuid.uuid4()), user_id=principal.user.id, name=body.name.strip(), prefix=prefix,
                 key_hash=key_hash, scopes=scopes, expires_at=expires_at)
    db.add(key)
    audit(db, "api_key.created", actor=principal.user, target_type="api_key", target_id=key.id,
          detail={"name": key.name, "scopes": scopes}, request=request, commit=False)
    db.commit()
    return ApiKeyCreated(api_key=plaintext, key=_out(key, principal.user.email))


@router.delete("/{key_id}", status_code=204, summary="Revoke an API key")
def revoke_key(key_id: str, request: Request, principal: Principal = Depends(require_user_session),
               db: Session = Depends(get_db)):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None or (key.user_id != principal.user.id and not principal.is_admin):
        raise HTTPException(status_code=404, detail="API key not found.")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        audit(db, "api_key.revoked", actor=principal.user, target_type="api_key", target_id=key.id,
              detail={"name": key.name}, request=request, commit=False)
        db.commit()
