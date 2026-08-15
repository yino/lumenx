from unittest.mock import Mock

import pytest

from src.platform.auth import (
    DuplicatePhoneError,
    InvalidPhoneError,
    PasswordPolicyError,
    PasswordService,
    ensure_phone_available,
    normalize_phone,
)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("13800138000", "+8613800138000"),
        ("+86 138 0013 8000", "+8613800138000"),
        ("0086 13800138000", "+8613800138000"),
    ],
)
def test_chinese_mobile_normalization(raw: str, canonical: str) -> None:
    assert normalize_phone(raw) == canonical


@pytest.mark.parametrize("raw", ["", "12345", "010-12345678", "+86 11111111111"])
def test_invalid_or_nonmobile_phone_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhoneError, match="手机号"):
        normalize_phone(raw)


def test_duplicate_phone_check_uses_canonical_value() -> None:
    session = Mock()
    session.scalar.return_value = "existing-user-id"

    with pytest.raises(DuplicatePhoneError, match="已注册"):
        ensure_phone_available(session, "+8613800138000")

    statement = session.scalar.call_args.args[0]
    assert "users.phone_canonical" in str(statement)


def test_password_policy_and_argon2id_hashing() -> None:
    service = PasswordService()

    with pytest.raises(PasswordPolicyError):
        service.hash("short1")
    with pytest.raises(PasswordPolicyError):
        service.hash("onlyletters")

    password_hash = service.hash("secure-pass-2026")

    assert password_hash.startswith("$argon2id$")
    assert "secure-pass-2026" not in password_hash
    assert service.verify(password_hash, "secure-pass-2026") is True
    assert service.verify(password_hash, "wrong-password") is False
    assert service.verify("invalid-hash", "secure-pass-2026") is False
