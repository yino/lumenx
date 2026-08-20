"""Credential resolution and plaintext-secret guards.

Only symbolic environment-variable references are allowed to cross the cloud
configuration boundary.  Secret values are resolved at the last possible
moment and wrapped in :class:`pydantic.SecretStr`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import SecretStr


SECRET_REFERENCE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,159}$")


class CredentialResolutionError(LookupError):
    """Raised when a configured credential reference cannot be resolved."""


def _normalized_key(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_sensitive_config_key(name: object) -> bool:
    """Return whether a field name is expected to contain a plaintext secret."""

    normalized = _normalized_key(name)
    if not normalized:
        return False

    # References are identifiers such as ``DASHSCOPE_API_KEY``, not the secret
    # values themselves.  They are intentionally safe to persist and audit.
    if normalized.endswith(("_ref", "_refs", "_reference", "_references")):
        return False

    exact = {
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "passphrase",
        "private_key",
        "secret",
        "secret_key",
        "session_secret",
        "signing_secret",
        "token",
    }
    if normalized in exact:
        return True

    sensitive_suffixes = (
        "_api_key",
        "_auth_token",
        "_access_token",
        "_refresh_token",
        "_access_key",
        "_access_key_id",
        "_access_key_secret",
        "_client_secret",
        "_password",
        "_passwd",
        "_passphrase",
        "_private_key",
        "_secret",
        "_secret_key",
        "_session_secret",
        "_signing_secret",
    )
    return normalized.endswith(sensitive_suffixes)


def reject_plaintext_secrets(value: Any, *, path: str = "数据") -> None:
    """Reject nested mappings containing fields that can hold raw secrets."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if is_sensitive_config_key(key):
                raise ValueError(f"{child_path} 不允许包含明文凭据字段")
            reject_plaintext_secrets(item, path=child_path)
        return

    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_plaintext_secrets(item, path=f"{path}[{index}]")


class EnvironmentCredentialProvider:
    """Resolve allow-listed secret references from a process environment."""

    def __init__(
        self,
        allowed_references: Iterable[str],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        references = tuple(str(reference).strip() for reference in allowed_references)
        invalid = [
            reference
            for reference in references
            if not SECRET_REFERENCE_PATTERN.fullmatch(reference)
        ]
        if invalid:
            raise ValueError("服务端凭据引用格式无效")
        self.allowed_references = frozenset(references)
        self.environment = environment if environment is not None else os.environ

    def resolve(self, secret_ref: str) -> SecretStr:
        normalized = str(secret_ref).strip()
        if not SECRET_REFERENCE_PATTERN.fullmatch(normalized):
            raise CredentialResolutionError("服务端凭据引用格式无效")
        if normalized not in self.allowed_references:
            raise CredentialResolutionError("服务端凭据引用未获部署配置授权")

        value = (self.environment.get(normalized) or "").strip()
        if not value:
            raise CredentialResolutionError("服务端凭据引用未解析")
        if value.lower().startswith(("your_", "change-me", "replace-with")):
            raise CredentialResolutionError("服务端凭据仍是占位值")
        return SecretStr(value)
