import base64
import hashlib
import hmac
import os

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 390000
_SALT_BYTES = 16


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(raw: str) -> bytes:
    return base64.b64decode(raw.encode("ascii"))


def hash_password(password: str) -> str:
    """Return a PBKDF2-SHA256 password hash string safe for database storage."""
    normalized_password = str(password)
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        normalized_password.encode("utf-8"),
        salt,
        _ITERATIONS,
    )
    return f"{_ALGORITHM}${_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Validate a plaintext password against the stored PBKDF2 hash string."""
    parts = str(stored_hash).strip().split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations_raw, salt_raw, digest_raw = parts
    if algorithm != _ALGORITHM:
        return False
    try:
        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected_digest = _b64decode(digest_raw)
    except Exception:
        return False
    candidate_digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(candidate_digest, expected_digest)
