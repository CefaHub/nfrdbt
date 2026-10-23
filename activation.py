"""Single-use activation code verification.

Only the SHA-256 digest is checked into the public repository.
The high-entropy bearer token itself must never be committed or logged.
The consumed flag and owner ID live in DATA_DIR/state.json.
"""
from __future__ import annotations

import hashlib
import hmac

ACTIVATION_KEY_SHA256 = "3607956fcd3553b795d8694f4baebf7559deefe4704169a7db85a36918485dda"


def valid_activation_key(code: str) -> bool:
    if not isinstance(code, str) or not 10 <= len(code) <= 128:
        return False
    actual = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, ACTIVATION_KEY_SHA256)
