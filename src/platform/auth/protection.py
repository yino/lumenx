from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from redis import Redis
from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..observability import events, metrics
from .sessions import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CookieSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, allowed_origins: frozenset[str]) -> None:
        super().__init__(app)
        self.allowed_origins = allowed_origins

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method not in UNSAFE_METHODS or SESSION_COOKIE_NAME not in request.cookies:
            return await call_next(request)

        origin = request.headers.get("origin", "").rstrip("/")
        if not origin or origin not in self.allowed_origins:
            return JSONResponse(
                status_code=403,
                content={"code": "ORIGIN_DENIED", "message": "请求来源不受信任"},
            )

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            return JSONResponse(
                status_code=403,
                content={"code": "CSRF_INVALID", "message": "安全校验失败，请刷新后重试"},
            )
        return await call_next(request)


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    maximum_attempts: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.maximum_attempts <= 0 or self.window_seconds <= 0:
            raise ValueError("限流次数和时间窗口必须为正整数")


class RateLimitExceeded(ValueError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("操作过于频繁，请稍后再试")
        self.retry_after_seconds = retry_after_seconds


class AuthRateLimiter:
    _INCREMENT_SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return count
    """

    def __init__(self, redis_client: Redis, key_secret: str) -> None:
        if len(key_secret) < 32:
            raise ValueError("限流键密钥至少需要 32 个字符")
        self.redis = redis_client
        self.key_secret = key_secret

    def fingerprint(self, value: str) -> str:
        return hmac.new(
            self.key_secret.encode("utf-8"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def check(
        self,
        action: str,
        policy: RateLimitPolicy,
        *,
        phone_canonical: str,
        network_source: str,
    ) -> None:
        labels = {"action": action}
        metrics.increment("auth_attempts_total", labels=labels)
        phone_fingerprint = self.fingerprint(phone_canonical)
        network_fingerprint = self.fingerprint(network_source)
        keys = (
            f"lumenx:auth-rate:{action}:phone:{phone_fingerprint}",
            f"lumenx:auth-rate:{action}:network:{network_fingerprint}",
        )
        for key in keys:
            count = int(
                self.redis.eval(
                    self._INCREMENT_SCRIPT,
                    1,
                    key,
                    policy.window_seconds,
                )
            )
            if count > policy.maximum_attempts:
                ttl = int(self.redis.ttl(key))
                metrics.increment("auth_rate_limited_total", labels=labels)
                events.emit(
                    "auth.rate_limited",
                    action=action,
                    phone_fingerprint=phone_fingerprint,
                    network_fingerprint=network_fingerprint,
                    retry_after_seconds=max(ttl, 1),
                )
                raise RateLimitExceeded(max(ttl, 1))
