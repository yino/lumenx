from __future__ import annotations

from dataclasses import dataclass
import re

import phonenumbers
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from phonenumbers import PhoneNumberFormat, PhoneNumberType
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db_models import UserRecord


class InvalidPhoneError(ValueError):
    pass


class InvalidUsernameError(ValueError):
    pass


class DuplicatePhoneError(ValueError):
    pass


class PasswordPolicyError(ValueError):
    pass


def normalize_phone(value: str, default_region: str = "CN") -> str:
    raw = value.strip()
    if not raw:
        raise InvalidPhoneError("请输入手机号")
    try:
        phone = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneError("手机号格式不正确") from exc

    number_type = phonenumbers.number_type(phone)
    mobile_types = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}
    if not phonenumbers.is_valid_number(phone) or number_type not in mobile_types:
        raise InvalidPhoneError("手机号格式不正确")
    return phonenumbers.format_number(phone, PhoneNumberFormat.E164)


def normalize_username(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", normalized):
        raise InvalidUsernameError("用户名格式不正确")
    return normalized


def normalize_login_identifier(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise InvalidPhoneError("请输入手机号或用户名")
    if re.search(r"[a-zA-Z_.-]", raw):
        return "username", normalize_username(raw)
    return "phone", normalize_phone(raw)


def ensure_phone_available(session: Session, phone_canonical: str) -> None:
    existing_id = session.scalar(
        select(UserRecord.id).where(UserRecord.phone_canonical == phone_canonical).limit(1)
    )
    if existing_id is not None:
        raise DuplicatePhoneError("该手机号已注册")


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    min_length: int = 10
    max_length: int = 128
    require_letter: bool = True
    require_digit: bool = True

    def validate(self, password: str) -> None:
        if len(password) < self.min_length:
            raise PasswordPolicyError(f"密码至少需要 {self.min_length} 位")
        if len(password) > self.max_length:
            raise PasswordPolicyError(f"密码不能超过 {self.max_length} 位")
        if self.require_letter and not any(character.isalpha() for character in password):
            raise PasswordPolicyError("密码至少需要包含一个字母")
        if self.require_digit and not any(character.isdigit() for character in password):
            raise PasswordPolicyError("密码至少需要包含一个数字")


class PasswordService:
    def __init__(self, policy: PasswordPolicy | None = None) -> None:
        self.policy = policy or PasswordPolicy()
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        self.policy.validate(password)
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (VerificationError, InvalidHashError):
            return True
