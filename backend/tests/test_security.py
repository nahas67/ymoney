"""Security primitives: passwords, JWT, secret encryption."""

from app.core.security import (
    create_access_token,
    decode_access_token,
    encrypt_secret,
    decrypt_secret,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip():
    h = hash_password("supersecret123")
    assert h != "supersecret123"
    assert verify_password("supersecret123", h)
    assert not verify_password("wrong-password", h)


def test_password_hashes_are_salted():
    a = hash_password("same-password-1")
    b = hash_password("same-password-1")
    assert a != b  # unique salts


def test_jwt_roundtrip():
    token = create_access_token("user-123")
    payload = decode_access_token(token)
    assert payload and payload["sub"] == "user-123"


def test_jwt_rejects_tampering():
    token = create_access_token("user-123")
    assert decode_access_token(token + "x") is None
    assert decode_access_token("") is None


def test_secret_encryption_roundtrip():
    ct = encrypt_secret("my-oauth-token")
    assert ct != "my-oauth-token"
    assert ct.startswith("v1:")
    assert decrypt_secret(ct) == "my-oauth-token"


def test_encrypt_produces_unique_ciphertexts():
    assert encrypt_secret("x") != encrypt_secret("x")


def test_decrypt_garbage_raises():
    import pytest

    with pytest.raises(Exception):
        decrypt_secret("v1:not-valid-base64!!!")
