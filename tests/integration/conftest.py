"""
Integration tests against a running stack. Run them inside the worker image so pypdfium2 and python-docx
are available to inspect the outputs:

    docker compose run --rm --no-deps -v ./tests:/app/tests -v ./dev_space:/app/dev_space worker python -m pytest tests/integration -v

Environment: IDP_API (default http://api:8000), IDP_ADMIN_EMAIL, IDP_ADMIN_PASSWORD (or SUPER_ADMIN_*).
"""
import json
import os
import time
import uuid

import pytest
import requests

API = os.environ.get("IDP_API", "http://api:8000").rstrip("/") + "/api/v1"
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Test documents live in one folder per service (see dev_space/test_docs/README.md): <name>.<ext> next to
# <name>.expected.json and, for extraction, <name>.schema.json. Raw outputs go to dev_space/results.
FIXTURES = os.environ.get("IDP_FIXTURES", os.path.join(_ROOT, "dev_space", "test_docs"))
PHI_DIR = os.path.join(FIXTURES, "phi_redaction")
PARSE_DIR = os.path.join(FIXTURES, "document_parsing")
EXTRACT_DIR = os.path.join(FIXTURES, "data_extraction")
RESULTS = os.environ.get("IDP_RESULTS", os.path.join(_ROOT, "dev_space", "results"))
os.makedirs(RESULTS, exist_ok=True)
with open(os.path.join(PHI_DIR, "custom_entities.json"), encoding="utf-8") as _f:
    CUSTOM_EMPLOYEE = json.load(_f)


def load_json(folder: str, name: str):
    with open(os.path.join(folder, name), encoding="utf-8") as f:
        return json.load(f)


def expected_for(folder: str) -> dict:
    """{document file name: its <name>.expected.json} for every document in a service folder."""
    out = {}
    names = os.listdir(folder)
    for name in names:
        if not name.endswith(".expected.json"):
            continue
        stem = name[: -len(".expected.json")]
        doc = next((n for n in names if os.path.splitext(n)[0] == stem and not n.endswith((".expected.json", ".schema.json"))), None)
        if doc:
            out[doc] = load_json(folder, name)
    return out


class Client:
    def __init__(self, token=None, api_key=None):
        self.s = requests.Session()
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        if api_key:
            self.s.headers["X-API-Key"] = api_key

    def get(self, path, **kw):
        return self.s.get(API + path, timeout=kw.pop("timeout", 60), **kw)

    def post(self, path, **kw):
        return self.s.post(API + path, timeout=kw.pop("timeout", 960), **kw)

    def patch(self, path, **kw):
        return self.s.patch(API + path, timeout=60, **kw)

    def put(self, path, **kw):
        return self.s.put(API + path, timeout=kw.pop("timeout", 60), **kw)

    def delete(self, path, **kw):
        return self.s.delete(API + path, timeout=60, **kw)

    def wait(self, job_id, timeout=900):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(f"/jobs/{job_id}").json()
            if job["status"] in ("COMPLETED", "FAILED"):
                return job
            time.sleep(0.5)
        raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def login(email, password):
    res = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    assert res.status_code == 200, res.text
    return res.json()


@pytest.fixture(scope="session")
def admin_credentials():
    email = os.environ.get("IDP_ADMIN_EMAIL") or os.environ.get("SUPER_ADMIN_EMAIL", "admin@idp.local")
    password = os.environ.get("IDP_ADMIN_PASSWORD") or os.environ["SUPER_ADMIN_PASSWORD"]
    return email, password


TEST_EMAIL_PREFIXES = ("it-", "x-")


@pytest.fixture(scope="session")
def admin(admin_credentials):
    client = Client(token=login(*admin_credentials)["access_token"])
    yield client
    # Remove every account the suite created so the user list stays clean between runs.
    fresh = Client(token=login(*admin_credentials)["access_token"])
    for user in fresh.get("/users").json():
        if user["email"].startswith(TEST_EMAIL_PREFIXES) and "super_admin" not in user["roles"]:
            fresh.delete(f"/users/{user['id']}")


def make_user(admin: Client, roles, password="Integration-Pass-42"):
    """Creates a user with a known password and signs in once to clear the temporary-password flag."""
    email = f"it-{'-'.join(roles).replace('_', '')}-{uuid.uuid4().hex[:6]}@idp.local"
    res = admin.post("/users", json={"email": email, "full_name": f"Test {' '.join(roles)}", "roles": roles,
                                     "password": password})
    assert res.status_code == 201, res.text
    session = login(email, password)
    client = Client(token=session["access_token"])
    res = client.post("/auth/change-password", json={"current_password": password, "new_password": password + "x"})
    assert res.status_code == 200, res.text
    client = Client(token=res.json()["access_token"])
    client.email, client.password, client.user_id = email, password + "x", res.json()["user"]["id"]
    return client


@pytest.fixture(scope="session")
def phi_user(admin):
    return make_user(admin, ["phi_redactor"])


@pytest.fixture(scope="session")
def parse_user(admin):
    return make_user(admin, ["document_parser"])


@pytest.fixture(scope="session")
def phi_api(phi_user):
    res = phi_user.post("/api-keys", json={"name": "integration-tests", "scopes": ["phi"], "expires_in_days": 1})
    assert res.status_code == 201, res.text
    return Client(api_key=res.json()["api_key"])


@pytest.fixture(scope="session")
def expected():
    return expected_for(PHI_DIR)


def record(name: str, data: dict):
    """Results are written next to the tests and summarised in the validation report."""
    with open(os.path.join(RESULTS, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
