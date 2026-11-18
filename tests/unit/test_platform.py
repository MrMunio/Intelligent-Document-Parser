import time

import pytest

from shared.formats import FORMAT_DOCX, FORMAT_IMAGE, FORMAT_PDF, FORMAT_TEXT, classify, safe_file_name
from shared.models import services_for_roles
from shared.security import (TOKEN_ACCESS, TOKEN_REFRESH, TokenError, create_token, decode_token, generate_api_key,
                             hash_api_key, hash_password, password_policy_error, verify_password)


def test_password_hash_roundtrip_and_policy():
    hashed = hash_password("Correct-horse-9")
    assert verify_password("Correct-horse-9", hashed) and not verify_password("wrong", hashed)
    assert password_policy_error("short1") and password_policy_error("onlyletterslong")
    assert password_policy_error("Abcdefgh12") is None


def test_jwt_type_and_expiry_are_enforced():
    token, ttl = create_token("u1", 3, TOKEN_ACCESS)
    payload = decode_token(token, TOKEN_ACCESS)
    assert payload["sub"] == "u1" and payload["ver"] == 3 and ttl > 0
    with pytest.raises(TokenError, match="type"):
        decode_token(token, TOKEN_REFRESH)
    with pytest.raises(TokenError):
        decode_token(token[:-2] + "xx", TOKEN_ACCESS)


def test_api_keys_are_random_and_only_the_hash_is_kept():
    key, prefix, digest = generate_api_key()
    assert key.startswith(prefix) and key.startswith("idp_") and len(key) > 40
    assert hash_api_key(key) == digest and generate_api_key()[0] != key


@pytest.mark.parametrize("roles, services", [
    (["document_parser"], {"parse"}), (["phi_redactor"], {"phi"}), (["admin"], {"parse", "phi"}),
    (["super_admin"], {"parse", "phi"}), (["document_parser", "phi_redactor"], {"parse", "phi"}), ([], set()),
])
def test_role_to_service_mapping(roles, services):
    assert services_for_roles(roles) == services


@pytest.mark.parametrize("name, head, expected", [
    ("a.pdf", b"%PDF-1.7", FORMAT_PDF), ("a.docx", b"PK\x03\x04", FORMAT_DOCX), ("a.PNG", b"\x89PNG", FORMAT_IMAGE),
    ("a.md", b"# hi", FORMAT_TEXT), ("a.hl7", b"MSH|^~\\&|", FORMAT_TEXT), ("noext", b"plain words", FORMAT_TEXT),
    ("a.xlsx", b"PK\x03\x04", None), ("a.docx", b"not zip", None), ("a.bin", b"\x00\x01\x02", None),
    ("weird.data", b"\x00\x00\xff\xfe\x00", None),
])
def test_format_classification(name, head, expected):
    assert classify(name, head) == expected


def test_safe_file_name_strips_paths_and_odd_characters():
    assert safe_file_name("../../etc/passwd") == "passwd"
    assert safe_file_name("C:\\Users\\x\\My <Report>.pdf") == "My _Report_.pdf"
    assert safe_file_name("") == "document"
