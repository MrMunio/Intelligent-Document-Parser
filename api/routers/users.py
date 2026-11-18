import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from api.deps import Principal, audit, require_admin
from api.routers.auth import user_out
from api.schemas import PasswordReset, UserCreate, UserCreated, UserOut, UserUpdate
from shared.database import get_db
from shared.models import ADMIN_ROLES, ALL_ROLES, ROLE_SUPER_ADMIN, User
from shared.security import generate_temp_password, hash_password, password_policy_error

router = APIRouter(prefix="/users", tags=["User management"])


def _validate_roles(roles: list[str]) -> list[str]:
    if not roles or any(r not in ALL_ROLES for r in roles):
        raise HTTPException(status_code=400, detail=f"Roles must be chosen from: {', '.join(ALL_ROLES)}.")
    return sorted(set(roles), key=ALL_ROLES.index)


def _guard_role_change(actor: Principal, target_roles_before: list[str], target_roles_after: list[str]) -> None:
    """Only a super admin may create, modify or remove administrator accounts."""
    touches_admin = bool(ADMIN_ROLES & (set(target_roles_before) | set(target_roles_after)))
    if touches_admin and not actor.is_super_admin:
        raise HTTPException(status_code=403, detail="Only a super admin can manage administrator accounts.")


def _super_admin_count(db: Session) -> int:
    users = db.query(User).filter(User.is_active.is_(True)).all()
    return sum(1 for u in users if ROLE_SUPER_ADMIN in (u.roles or []))


@router.get("", response_model=list[UserOut], summary="List all users")
def list_users(_: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    return [user_out(u) for u in db.query(User).order_by(User.created_at.asc()).all()]


@router.post("", response_model=UserCreated, status_code=201, summary="Create a user and assign roles")
def create_user(body: UserCreate, request: Request, actor: Principal = Depends(require_admin),
                db: Session = Depends(get_db)):
    roles = _validate_roles(body.roles)
    _guard_role_change(actor, [], roles)
    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="A user with this email already exists.")

    temp_password = None
    if body.password:
        problem = password_policy_error(body.password)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        password = body.password
    else:
        password = temp_password = generate_temp_password()

    user = User(id=str(uuid.uuid4()), email=email, full_name=body.full_name.strip(), roles=roles,
                password_hash=hash_password(password), is_active=True, must_change_password=True,
                created_by=actor.user.id)
    db.add(user)
    audit(db, "user.created", actor=actor.user, target_type="user", target_id=user.id,
          detail={"email": email, "roles": roles}, request=request, commit=False)
    db.commit()
    return UserCreated(user=user_out(user), temporary_password=temp_password)


@router.patch("/{user_id}", response_model=UserOut, summary="Update name, roles or active state")
def update_user(user_id: str, body: UserUpdate, request: Request, actor: Principal = Depends(require_admin),
                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    before = list(user.roles or [])
    after = _validate_roles(body.roles) if body.roles is not None else before
    _guard_role_change(actor, before, after)

    removing_super = ROLE_SUPER_ADMIN in before and (ROLE_SUPER_ADMIN not in after or body.is_active is False)
    if removing_super and user.is_active and _super_admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="The last active super admin cannot be demoted or disabled.")
    if user.id == actor.user.id and body.is_active is False:
        raise HTTPException(status_code=400, detail="You cannot disable your own account.")

    changes = {}
    if body.full_name is not None and body.full_name.strip() != user.full_name:
        user.full_name = body.full_name.strip()
        changes["full_name"] = user.full_name
    if after != before:
        user.roles = after
        user.token_version += 1
        changes["roles"] = {"from": before, "to": after}
    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        user.token_version += 1
        changes["is_active"] = body.is_active
    if changes:
        audit(db, "user.updated", actor=actor.user, target_type="user", target_id=user.id,
              detail={"email": user.email, **changes}, request=request, commit=False)
        db.commit()
    return user_out(user)


@router.post("/{user_id}/reset-password", response_model=PasswordReset, summary="Issue a temporary password")
def reset_password(user_id: str, request: Request, actor: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    _guard_role_change(actor, list(user.roles or []), list(user.roles or []))
    temp = generate_temp_password()
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    user.token_version += 1
    audit(db, "user.password_reset", actor=actor.user, target_type="user", target_id=user.id,
          detail={"email": user.email}, request=request, commit=False)
    db.commit()
    return PasswordReset(temporary_password=temp)


@router.delete("/{user_id}", status_code=204, summary="Delete a user")
def delete_user(user_id: str, request: Request, actor: Principal = Depends(require_admin),
                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.id == actor.user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    _guard_role_change(actor, list(user.roles or []), [])
    if ROLE_SUPER_ADMIN in (user.roles or []) and user.is_active and _super_admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="The last active super admin cannot be deleted.")
    audit(db, "user.deleted", actor=actor.user, target_type="user", target_id=user.id,
          detail={"email": user.email}, request=request, commit=False)
    db.delete(user)
    db.commit()
