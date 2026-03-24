from app.core.passwords import hash_password, verify_password


def test_hash_password_roundtrip():
    hashed = hash_password("top-secret-value")
    assert hashed.startswith("pbkdf2_sha256$")
    assert verify_password("top-secret-value", hashed) is True


def test_verify_password_rejects_invalid_value():
    hashed = hash_password("top-secret-value")
    assert verify_password("wrong-value", hashed) is False
    assert verify_password("top-secret-value", "invalid-format") is False
