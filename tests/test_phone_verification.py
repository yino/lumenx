import pytest

from src.platform.auth.verification import (
    DisabledPhoneVerificationProvider,
    PhoneVerificationProvider,
    PhoneVerificationUnavailable,
    VerificationPurpose,
)


def test_disabled_provider_satisfies_reserved_contract() -> None:
    provider = DisabledPhoneVerificationProvider()

    assert isinstance(provider, PhoneVerificationProvider)
    with pytest.raises(PhoneVerificationUnavailable, match="暂不支持短信"):
        provider.send_code("+8613800138000", VerificationPurpose.VERIFY_PHONE)
    with pytest.raises(PhoneVerificationUnavailable, match="联系平台管理员"):
        provider.verify_code("challenge", "123456", VerificationPurpose.RECOVER_PASSWORD)
