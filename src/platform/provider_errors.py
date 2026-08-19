from __future__ import annotations


class ProviderTerminalFailureError(RuntimeError):
    """The provider accepted a task and later reported a terminal failure."""

    def __init__(self, message: str, *, provider_status: str) -> None:
        super().__init__(message)
        self.provider_status = provider_status


class ProviderRequestRejectedError(RuntimeError):
    """The provider rejected a request before creating a billable task."""

    def __init__(
        self,
        message: str,
        *,
        provider_code: str,
        safe_error_code: str,
        safe_error_message: str,
    ) -> None:
        super().__init__(message)
        self.provider_code = provider_code
        self.safe_error_code = safe_error_code
        self.safe_error_message = safe_error_message
