import re
from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import AfterValidator, BaseModel, Field

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def _check_email(value: str) -> str:
    # Plain format check. Internal directories often use domains such as .local or .corp,
    # which strict validators reject as non-deliverable.
    value = value.strip().lower()
    if len(value) > 254 or not _EMAIL.match(value):
        raise ValueError("Enter a valid email address.")
    return value


EmailStr = Annotated[str, AfterValidator(_check_email)]


# ---- Auth ---------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    roles: list[str]
    services: list[str]
    is_active: bool
    must_change_password: bool
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


# ---- Users --------------------------------------------------------------------

class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    roles: list[str] = Field(min_length=1)
    password: Optional[str] = Field(default=None, max_length=256)


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    roles: Optional[list[str]] = None
    is_active: Optional[bool] = None


class UserCreated(BaseModel):
    user: UserOut
    temporary_password: Optional[str] = None


class PasswordReset(BaseModel):
    temporary_password: str


# ---- API keys -----------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    owner_email: Optional[str] = None


class ApiKeyCreated(BaseModel):
    api_key: str
    key: ApiKeyOut


# ---- PHI ----------------------------------------------------------------------

class CustomEntity(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=600)


class RedactTextRequest(BaseModel):
    text: str = Field(min_length=1)
    file_name: Optional[str] = Field(default=None, max_length=200)
    entity_types: Optional[list[str]] = None
    custom_entities: list[CustomEntity] = Field(default_factory=list)
    instructions: Optional[str] = Field(default=None, max_length=2000)
    redaction_style: str = "label"
    use_pattern_detectors: bool = True


# ---- Data extraction ----------------------------------------------------------

class ValidateSpecRequest(BaseModel):
    spec: Any = Field(description="The extraction spec: a JSON object with 'properties' (or a JSON string of it).")


class ExtractTextRequest(BaseModel):
    text: str = Field(min_length=1, description="The document text.")
    spec: Any = Field(description="The extraction spec: a JSON object with 'properties' (or a JSON string of it).")
    approach: str = Field(default="agentic", description="'batch' or 'agentic'.")
    model: Optional[str] = Field(default=None, description="'provider:model' from GET /extract/options; default: configured engine.")
    instructions: Optional[str] = Field(default=None, max_length=4000)
    context_window: Optional[int] = Field(default=None, description="Cap on the usable context window in tokens (min 16000).")
    file_name: Optional[str] = Field(default=None, max_length=200)


# ---- Jobs ---------------------------------------------------------------------

class JobOut(BaseModel):
    job_id: str
    job_type: str
    status: str
    file_name: Optional[str] = None
    input_format: Optional[str] = None
    file_size: Optional[int] = None
    source: Optional[str] = None
    options: Optional[dict[str, Any]] = None
    progress: Optional[dict[str, Any]] = None
    stats: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    owner_email: Optional[str] = None
    result_file_name: Optional[str] = None
    result_media_type: Optional[str] = None
    result_available: bool = False
    links: dict[str, str] = Field(default_factory=dict)
    usage: Optional[dict[str, Any]] = Field(default=None, description="Language model calls, tokens and cost in USD "
                                                                     "for this job: total and by_model")
    # Present on single-job responses only
    result_data: Optional[Any] = None
    result_markdown: Optional[str] = None


class JobAccepted(BaseModel):
    job_id: str
    status: str
    message: str
    links: dict[str, str]


class JobList(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int
