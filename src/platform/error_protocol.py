from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .observability import events, metrics

CORRELATION_HEADER = "X-Correlation-ID"
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_HAS_CHINESE = re.compile(r"[\u3400-\u9fff]")

_STATUS_ERRORS: dict[int, tuple[str, str]] = {
    400: ("REQUEST_INVALID", "请求参数无效，请检查后重试"),
    401: ("AUTH_REQUIRED", "登录状态已失效，请重新登录"),
    402: ("TICKET_BALANCE_INSUFFICIENT", "算力券余额不足"),
    403: ("ACCESS_DENIED", "当前操作没有权限"),
    404: ("RESOURCE_NOT_FOUND", "请求的内容不存在或无权访问"),
    405: ("METHOD_NOT_ALLOWED", "当前请求方式不受支持"),
    409: ("REQUEST_CONFLICT", "内容已发生变化，请刷新后重试"),
    413: ("PAYLOAD_TOO_LARGE", "提交的内容过大，请调整后重试"),
    422: ("VALIDATION_ERROR", "提交的信息不符合要求，请检查后重试"),
    428: ("CONTENT_VERSION_REQUIRED", "内容版本信息缺失，请刷新后重试"),
    429: ("RATE_LIMITED", "操作过于频繁，请稍后再试"),
    500: ("INTERNAL_ERROR", "服务暂时不可用，请稍后重试"),
    503: ("SERVICE_UNAVAILABLE", "服务暂时不可用，请稍后重试"),
}


def request_correlation_id(request: Request) -> str:
    current = getattr(request.state, "correlation_id", None)
    if isinstance(current, str) and current:
        return current
    supplied = request.headers.get(CORRELATION_HEADER)
    correlation_id = (
        supplied
        if supplied and _CORRELATION_PATTERN.fullmatch(supplied)
        else str(uuid.uuid4())
    )
    request.state.correlation_id = correlation_id
    return correlation_id


def _status_error(status_code: int) -> tuple[str, str]:
    if status_code in _STATUS_ERRORS:
        return _STATUS_ERRORS[status_code]
    if status_code >= 500:
        return _STATUS_ERRORS[500]
    return "REQUEST_FAILED", "暂时无法完成操作，请稍后重试"


def _http_error(exc: StarletteHTTPException) -> tuple[str, str]:
    fallback_code, fallback_message = _status_error(exc.status_code)
    if isinstance(exc.detail, Mapping):
        raw_code = exc.detail.get("code")
        raw_message = exc.detail.get("message")
        code = raw_code if isinstance(raw_code, str) and raw_code else fallback_code
        message = (
            raw_message
            if isinstance(raw_message, str) and _HAS_CHINESE.search(raw_message)
            else fallback_message
        )
        return code, message
    if isinstance(exc.detail, str) and _HAS_CHINESE.search(exc.detail):
        return fallback_code, exc.detail
    return fallback_code, fallback_message


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    correlation_id = request_correlation_id(request)
    response_headers = dict(headers or {})
    response_headers[CORRELATION_HEADER] = correlation_id
    metrics.increment(
        "api_errors_total",
        labels={"error_code": code, "status": str(status_code)},
    )
    return JSONResponse(
        status_code=status_code,
        headers=response_headers,
        content={
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
        },
    )


def install_cloud_error_protocol(
    app: FastAPI,
    *,
    logger: logging.Logger | None = None,
) -> None:
    error_logger = logger or logging.getLogger(__name__)

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id = request_correlation_id(request)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - this is the final safe boundary.
            events.emit(
                "api.unhandled_error",
                correlation_id=correlation_id,
                method=request.method,
                path=request.url.path,
                error_type=type(exc).__name__,
            )
            error_logger.error(
                "Unhandled API error correlation_id=%s method=%s path=%s type=%s",
                correlation_id,
                request.method,
                request.url.path,
                type(exc).__name__,
                exc_info=True,
            )
            return _error_response(
                request,
                status_code=500,
                code="INTERNAL_ERROR",
                message="服务暂时不可用，请稍后重试",
            )
        if response.status_code in {403, 404} and request.headers.get("x-workspace-id"):
            outcome = "denied" if response.status_code == 403 else "hidden_or_missing"
            metrics.increment(
                "workspace_authorization_denials_total",
                labels={"outcome": outcome, "resource": "workspace_scoped"},
            )
            events.emit(
                "workspace.access_denied",
                correlation_id=correlation_id,
                workspace_id=request.headers.get("x-workspace-id"),
                method=request.method,
                path=request.url.path,
                status=response.status_code,
            )
        response.headers[CORRELATION_HEADER] = correlation_id
        return response

    @app.exception_handler(StarletteHTTPException)
    def handle_http_exception(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        code, message = _http_error(exc)
        return _error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    def handle_request_validation(
        request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="VALIDATION_ERROR",
            message="提交的信息不符合要求，请检查后重试",
        )
