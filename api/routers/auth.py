from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from api.deps import Principal, audit, client_ip, get_principal, require_user_session
from api.schemas import ChangePasswordRequest, LoginRequest, TokenResponse, UserOut
from shared.config import settings
from shared.database import get_db
from shared.models import User, services_for_roles
from shared.queue import redis_conn
from shared.security import (TOKEN_ACCESS, TOKEN_REFRESH, TokenError, create_token, decode_token,
                             hash_password, password_policy_error, verify_password)

router = APIRouter(prefix="/auth", tags=["Authentication"])

REFRESH_COOKIE = "idp_refresh"
COOKIE_PATH = "/api/v1/auth"
# Evaluated when the account does not exist so response time does not reveal which emails are registered.
_DUMMY_HASH = hash_password("timing-equalisation-only")


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, email=user.email, full_name=user.full_name, roles=list(user.roles or []),
        services=sorted(services_for_roles(user.roles)), is_active=user.is_active,
        must_change_password=user.must_change_password, last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


def _set_refresh_cookie(response: Response, user: User) -> None:
    token, ttl = create_token(user.id, user.token_version, TOKEN_REFRESH)
    response.set_cookie(REFRESH_COOKIE, token, max_age=ttl, httponly=True, samesite="strict",
                        secure=settings.cookie_secure, path=COOKIE_PATH)


def _token_response(user: User) -> TokenResponse:
    access, ttl = create_token(user.id, user.token_version, TOKEN_ACCESS)
    return TokenResponse(access_token=access, expires_in=ttl, user=user_out(user))


def _fail_key(email: str) -> str:
    return f"idp:login_fail:{email}"


@router.post("/login", response_model=TokenResponse, summary="Sign in with email and password")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    key = _fail_key(email)
    try:
        failures = int(redis_conn.get(key) or 0)
    except Exception:
        failures = 0  # the limiter must not take sign-in down with it
    if failures >= settings.login_max_failures:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in "
                                                    f"{settings.login_lockout_minutes} minutes.")

    user = db.query(User).filter(User.email == email).first()
    valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid or not user.is_active:
        try:
            pipe = redis_conn.pipeline()
            pipe.incr(key)
            pipe.expire(key, settings.login_lockout_minutes * 60)
            pipe.execute()
        except Exception:
            pass
        audit(db, "auth.login_failed", actor_email=email, request=request)
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    try:
        redis_conn.delete(key)
    except Exception:
        pass
    user.last_login_at = datetime.now(timezone.utc)
    audit(db, "auth.login", actor=user, request=request, commit=False)
    db.commit()
    _set_refresh_cookie(response, user)
    return _token_response(user)


@router.post("/refresh", response_model=TokenResponse, summary="Exchange the refresh cookie for a new access token")
def refresh(response: Response, db: Session = Depends(get_db),
            refresh_cookie: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE)):
    if not refresh_cookie:
        raise HTTPException(status_code=401, detail="No active session.")
    try:
        payload = decode_token(refresh_cookie, TOKEN_REFRESH)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if user is None or not user.is_active or payload.get("ver") != user.token_version:
        raise HTTPException(status_code=401, detail="Session is no longer valid. Please sign in again.")
    _set_refresh_cookie(response, user)  # rotate
    return _token_response(user)


@router.post("/logout", status_code=204, summary="End the session")
def logout(response: Response):
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut, summary="Current account, roles and service access")
def me(principal: Principal = Depends(get_principal)):
    out = user_out(principal.user)
    if principal.api_key is not None:
        out.services = sorted(principal.services)
    return out


@router.post("/change-password", response_model=TokenResponse, summary="Change your own password")
def change_password(body: ChangePasswordRequest, request: Request, response: Response,
                    principal: Principal = Depends(require_user_session), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == principal.user.id).first()
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    problem = password_policy_error(body.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="New password must be different from the current password.")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.token_version += 1  # signs out every other session
    audit(db, "auth.password_changed", actor=user, request=request, commit=False)
    db.commit()
    _set_refresh_cookie(response, user)
    return _token_response(user)
