"""Key rotation: same plaintext under the new key, and a wrong old key changes nothing."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.cli.maintenance import _fernet, reencrypt


def test_reencrypt_moves_the_plaintext_to_the_new_key() -> None:
    old, new = Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key())
    blob = old.encrypt(b'{"password": "s3cret"}')
    rotated = reencrypt(blob, old, new)
    assert new.decrypt(rotated) == b'{"password": "s3cret"}'
    with pytest.raises(InvalidToken):
        old.decrypt(rotated)


def test_reencrypt_refuses_a_blob_that_is_not_under_the_old_key() -> None:
    other, old, new = (Fernet(Fernet.generate_key()) for _ in range(3))
    with pytest.raises(InvalidToken):
        reencrypt(other.encrypt(b"x"), old, new)


@pytest.mark.parametrize("value", [None, "", "not-a-key"])
def test_a_missing_or_malformed_key_is_a_clear_error(value: str | None) -> None:
    with pytest.raises(ValueError, match="KEY"):
        _fernet("SOME_KEY", value)
