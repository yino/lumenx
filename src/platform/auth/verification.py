from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class VerificationPurpose(str, Enum):
    REGISTER = "register"
    VERIFY_PHONE = "verify_phone"
    RECOVER_PASSWORD = "recover_password"


@dataclass(frozen=True, slots=True)
class VerificationChallenge:
    challenge_id: str
    expires_in_seconds: int


class PhoneVerificationUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("当前版本暂不支持短信验证或短信找回，请联系平台管理员")


@runtime_checkable
class PhoneVerificationProvider(Protocol):
    def send_code(
        self,
        phone_canonical: str,
        purpose: VerificationPurpose,
    ) -> VerificationChallenge: ...

    def verify_code(
        self,
        challenge_id: str,
        code: str,
        purpose: VerificationPurpose,
    ) -> bool: ...


class DisabledPhoneVerificationProvider:
    def send_code(
        self,
        phone_canonical: str,
        purpose: VerificationPurpose,
    ) -> VerificationChallenge:
        raise PhoneVerificationUnavailable

    def verify_code(
        self,
        challenge_id: str,
        code: str,
        purpose: VerificationPurpose,
    ) -> bool:
        raise PhoneVerificationUnavailable
