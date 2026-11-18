import uuid

import requests

from tests.integration.conftest import API, Client, login, make_user


def test_health_ready_and_openapi_are_public():
    base = API.rsplit("/api/v1", 1)[0]
    assert requests.get(f"{base}/health", timeout=10).json() == {"status": "ok"}
    ready = requests.get(f"{base}/ready", timeout=10).json()
    assert ready["database"] and ready["redis"]
    spec = requests.get(f"{base}/api/openapi.json", timeout=10).json()
    for path in ("/api/v1/phi/jobs", "/api/v1/phi/redact", "/api/v1/phi/redact-text", "/api/v1/parse/jobs",
                 "/api/v1/parse/sync", "/api/v1/jobs/{job_id}/file", "/api/v1/jobs/{job_id}/metadata"):
        assert path in spec["paths"], path


def test_every_service_endpoint_requires_authentication():
    for method, path in [("get", "/jobs"), ("get", "/phi/entity-types"), ("post", "/phi/jobs"), ("post", "/parse/jobs"),
                         ("get", "/users"), ("get", "/admin/system"), ("get", "/api-keys")]:
        assert getattr(requests, method)(API + path, timeout=10).status_code == 401, path
    bad = requests.get(API + "/jobs", headers={"Authorization": "Bearer not-a-token"}, timeout=10)
    assert bad.status_code == 401
    assert requests.get(API + "/jobs", headers={"X-API-Key": "idp_deadbeef_nope"}, timeout=10).status_code == 401


def test_login_refresh_cookie_and_logout(admin_credentials):
    s = requests.Session()
    res = s.post(f"{API}/auth/login", json={"email": admin_credentials[0], "password": admin_credentials[1]}, timeout=30)
    assert res.status_code == 200
    cookie = res.headers.get("set-cookie", "")
    assert "idp_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    refreshed = s.post(f"{API}/auth/refresh", timeout=30)
    assert refreshed.status_code == 200 and refreshed.json()["access_token"]
    s.post(f"{API}/auth/logout", timeout=30)
    assert s.post(f"{API}/auth/refresh", timeout=30).status_code == 401


def test_wrong_password_is_rejected_and_lockout_applies(admin):
    user = make_user(admin, ["phi_redactor"])
    for _ in range(5):
        assert requests.post(f"{API}/auth/login", json={"email": user.email, "password": "wrong-password-1"},
                             timeout=30).status_code == 401
    locked = requests.post(f"{API}/auth/login", json={"email": user.email, "password": user.password}, timeout=30)
    assert locked.status_code == 429


def test_unknown_email_gets_the_same_message_as_a_wrong_password():
    res = requests.post(f"{API}/auth/login", json={"email": f"nobody-{uuid.uuid4().hex[:6]}@idp.local",
                                                   "password": "whatever-123"}, timeout=30)
    assert res.status_code == 401 and res.json()["detail"] == "Incorrect email or password."


def test_temporary_password_blocks_services_until_changed(admin):
    email = f"it-temp-{uuid.uuid4().hex[:6]}@idp.local"
    created = admin.post("/users", json={"email": email, "full_name": "Temp User", "roles": ["phi_redactor"]}).json()
    temp = created["temporary_password"]
    assert temp and created["user"]["must_change_password"]
    client = Client(token=login(email, temp)["access_token"])
    assert client.get("/phi/entity-types").status_code == 403
    weak = client.post("/auth/change-password", json={"current_password": temp, "new_password": "short"})
    assert weak.status_code == 400
    ok = client.post("/auth/change-password", json={"current_password": temp, "new_password": "A-much-better-pass-7"})
    assert ok.status_code == 200
    # The old token is revoked by the password change, the new one works.
    assert client.get("/phi/entity-types").status_code == 401
    assert Client(token=ok.json()["access_token"]).get("/phi/entity-types").status_code == 200


def test_document_parser_role_cannot_use_phi(parse_user):
    assert parse_user.get("/phi/entity-types").status_code == 403
    files = {"file": ("x.txt", b"Patient Sarah Mitchell", "text/plain")}
    assert parse_user.post("/phi/jobs", files=files).status_code == 403
    assert parse_user.post("/phi/redact-text", json={"text": "Sarah Mitchell"}).status_code == 403
    assert parse_user.get("/users").status_code == 403
    assert parse_user.get("/admin/system").status_code == 403
    me = parse_user.get("/auth/me").json()
    assert me["services"] == ["parse"]


def test_phi_redactor_role_cannot_use_parsing(phi_user):
    files = {"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, "image/png")}
    assert phi_user.post("/parse/jobs", files=files).status_code == 403
    assert phi_user.get("/users").status_code == 403
    assert phi_user.get("/auth/me").json()["services"] == ["phi"]


def test_users_cannot_see_each_others_jobs(admin, phi_user):
    other = make_user(admin, ["phi_redactor"])
    job = other.post("/phi/jobs", files={"file": ("n.txt", b"Patient Sarah Mitchell", "text/plain")}).json()
    assert phi_user.get(f"/jobs/{job['job_id']}").status_code == 404
    assert all(j["job_id"] != job["job_id"] for j in phi_user.get("/jobs").json()["items"])
    assert admin.get(f"/jobs/{job['job_id']}").status_code == 200  # administrators can audit any job
    all_jobs = admin.get("/jobs?scope=all&page_size=100").json()["items"]
    assert any(j["job_id"] == job["job_id"] for j in all_jobs)
    other.wait(job["job_id"])


def test_admin_cannot_create_or_modify_administrators(admin):
    plain_admin = make_user(admin, ["admin"])
    res = plain_admin.post("/users", json={"email": f"x-{uuid.uuid4().hex[:5]}@idp.local", "full_name": "X", "roles": ["admin"]})
    assert res.status_code == 403
    ok = plain_admin.post("/users", json={"email": f"x-{uuid.uuid4().hex[:5]}@idp.local", "full_name": "Y",
                                          "roles": ["document_parser"]})
    assert ok.status_code == 201
    supers = [u for u in admin.get("/users").json() if "super_admin" in u["roles"]]
    assert plain_admin.patch(f"/users/{supers[0]['id']}", json={"is_active": False}).status_code == 403
    assert plain_admin.delete(f"/users/{supers[0]['id']}").status_code == 403


def test_multiple_admins_and_last_super_admin_protection(admin):
    second = make_user(admin, ["admin"])
    third = make_user(admin, ["admin"])
    assert second.get("/users").status_code == 200 and third.get("/users").status_code == 200
    me = admin.get("/auth/me").json()
    assert admin.delete(f"/users/{me['id']}").status_code == 400  # cannot delete yourself
    super_admins = [u for u in admin.get("/users").json() if "super_admin" in u["roles"] and u["is_active"]]
    if len(super_admins) == 1:
        res = admin.patch(f"/users/{me['id']}", json={"roles": ["admin"]})
        assert res.status_code == 400 and "last active super admin" in res.json()["detail"]


def test_role_change_takes_effect_immediately(admin):
    user = make_user(admin, ["phi_redactor"])
    assert user.get("/phi/entity-types").status_code == 200
    admin.patch(f"/users/{user.user_id}", json={"roles": ["document_parser"]})
    assert user.get("/phi/entity-types").status_code == 401  # existing sessions are invalidated
    fresh = Client(token=login(user.email, user.password)["access_token"])
    assert fresh.get("/phi/entity-types").status_code == 403


def test_disabled_user_cannot_sign_in_and_their_keys_stop(admin):
    user = make_user(admin, ["phi_redactor"])
    key = user.post("/api-keys", json={"name": "k", "scopes": ["phi"]}).json()["api_key"]
    assert Client(api_key=key).get("/phi/entity-types").status_code == 200
    admin.patch(f"/users/{user.user_id}", json={"is_active": False})
    assert Client(api_key=key).get("/phi/entity-types").status_code == 401
    assert requests.post(f"{API}/auth/login", json={"email": user.email, "password": user.password}, timeout=30).status_code == 401


def test_api_key_scopes_lifecycle_and_limits(admin, phi_user):
    assert phi_user.post("/api-keys", json={"name": "too much", "scopes": ["phi", "parse"]}).status_code == 403
    created = admin.post("/api-keys", json={"name": "parse only", "scopes": ["parse"]}).json()
    key = Client(api_key=created["api_key"])
    assert key.get("/jobs").status_code == 200
    assert key.get("/phi/entity-types").status_code == 403  # scope narrower than the owner's roles
    assert key.get("/users").status_code == 403  # keys can never manage accounts
    assert key.post("/api-keys", json={"name": "x", "scopes": ["parse"]}).status_code == 403
    listed = admin.get("/api-keys").json()
    assert all("api_key" not in k and "key_hash" not in k for k in listed)
    assert admin.delete(f"/api-keys/{created['key']['id']}").status_code == 204
    assert key.get("/jobs").status_code == 401
    bearer = Client(token=created["api_key"])  # keys are also accepted as a Bearer token
    assert bearer.get("/jobs").status_code == 401


def test_audit_log_records_security_events(admin):
    entries = admin.get("/admin/audit-logs?page_size=200").json()["items"]
    actions = {e["action"] for e in entries}
    assert {"auth.login", "auth.login_failed", "user.created", "api_key.created", "job.submitted"} <= actions
