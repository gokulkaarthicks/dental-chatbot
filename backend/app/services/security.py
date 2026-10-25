"""One-way password hashing using the standard library.

Only demo staff accounts exist, so a well-tuned PBKDF2-HMAC-SHA256 digest avoids
pulling in a native dependency. Format: ``pbkdf2_sha256$<iterations>$<salt>$<hash>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "$".join(
        (
            ALGORITHM,
            str(ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def hash_token(token: str) -> str:
    """Session tokens are stored hashed so a DB leak does not expose them."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
