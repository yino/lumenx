"""
LLM Adapter - Unified interface for DashScope and OpenAI-compatible APIs.

Supports two providers:
  - dashscope (default): Alibaba Cloud DashScope via OpenAI-compatible endpoint
  - openai: Any OpenAI-compatible API (OpenAI, DeepSeek, Ollama, etc.)

Configuration via environment variables:
  LLM_PROVIDER=dashscope|openai
  DASHSCOPE_API_KEY=...
  OPENAI_API_KEY=...
  OPENAI_BASE_URL=https://api.openai.com/v1
  OPENAI_MODEL=gpt-4o
"""
import os
import logging
from typing import Dict, List, Optional, Any

from ...utils.endpoints import get_provider_base_url
from ...models.provider_result import ProviderTextResult

logger = logging.getLogger(__name__)


class LLMAdapter:
    """Unified LLM call interface supporting DashScope and OpenAI-compatible APIs."""

    def __init__(
        self,
        *,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "dashscope")).lower()
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = None
        logger.info(f"LLM Adapter initialized with provider: {self.provider}")

    @property
    def is_configured(self) -> bool:
        if self.api_key:
            return True
        if self.provider == "openai":
            return bool(os.getenv("OPENAI_API_KEY"))
        return bool(os.getenv("DASHSCOPE_API_KEY"))

    def _get_client(self):
        """Get or create the OpenAI-compatible client (lazy, cached)."""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. Run: pip install openai>=1.0.0"
                )

            if self.provider == "openai":
                self._client = OpenAI(
                    api_key=self.api_key or os.getenv("OPENAI_API_KEY"),
                    base_url=(
                        self.base_url
                        or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
                    ),
                )
            else:
                # DashScope uses OpenAI-compatible endpoint
                self._client = OpenAI(
                    api_key=self.api_key or os.getenv("DASHSCOPE_API_KEY"),
                    base_url=(
                        self.base_url
                        or f"{get_provider_base_url('DASHSCOPE')}/compatible-mode/v1"
                    ),
                )
        return self._client

    # DashScope qwen 系列：首选 qwen3.7-plus（最新），不可用时回退到 qwen3.6-plus，
    # 最终回退到 qwen-plus alias（始终指向最新稳定通用版）。
    # 维护 fallback chain 而不是硬写一个名字，避免新版本上下线时整条 LLM 链断掉。
    _DASHSCOPE_MODEL_FALLBACK_CHAIN = ["qwen3.7-plus", "qwen3.6-plus", "qwen-plus"]

    def _get_default_model(self) -> str:
        if self.model:
            return self.model
        if self.provider == "openai":
            return os.getenv("OPENAI_MODEL", "gpt-4o")
        return self._DASHSCOPE_MODEL_FALLBACK_CHAIN[0]

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        response_format: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> str:
        """
        Send a chat completion request and return the response content.

        Args:
            messages: List of {"role": ..., "content": ...} dicts
            model: Model name override (uses provider default if None)
            response_format: Optional {"type": "json_object"} constraint

        Returns:
            The assistant's response content as a string.

        Raises:
            RuntimeError: If the API call fails.
        """
        return self.chat_with_usage(
            messages,
            model=model,
            response_format=response_format,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        ).content

    def chat_with_usage(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        response_format: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> ProviderTextResult:
        client = self._get_client()

        # 显式 model override 路径：单次尝试，失败就抛。
        if model:
            return self._chat_once_with_usage(
                client,
                model,
                messages,
                response_format,
                max_tokens,
                enable_thinking,
            )

        # Provider 默认路径：DashScope 走 fallback chain，OpenAI 单次尝试。
        if self.provider == "openai":
            return self._chat_once_with_usage(
                client,
                self._get_default_model(),
                messages,
                response_format,
                max_tokens,
                enable_thinking,
            )

        last_err: Optional[Exception] = None
        for idx, candidate in enumerate(self._DASHSCOPE_MODEL_FALLBACK_CHAIN):
            try:
                return self._chat_once_with_usage(
                    client,
                    candidate,
                    messages,
                    response_format,
                    max_tokens,
                    enable_thinking,
                )
            except RuntimeError as e:
                # 仅在 "模型不存在 / 不可用" 类错误时回退；其他错误（鉴权、限流、网络）
                # 直接抛，不浪费第二次重试。判定关键字宽松匹配 DashScope 文案。
                msg = str(e).lower()
                is_model_unavailable = any(k in msg for k in (
                    "model not found", "invalidmodel", "model_not_found",
                    "no such model", "not supported", "modelnotfound", "404",
                ))
                last_err = e
                if is_model_unavailable and idx < len(self._DASHSCOPE_MODEL_FALLBACK_CHAIN) - 1:
                    next_candidate = self._DASHSCOPE_MODEL_FALLBACK_CHAIN[idx + 1]
                    logger.warning(
                        "DashScope model %s unavailable (%s); falling back to %s",
                        candidate, e, next_candidate,
                    )
                    continue
                raise
        # 理论上不可达（最后一次失败已 raise），保留兜底
        raise last_err if last_err else RuntimeError("DashScope: no models available")

    def _chat_once(
        self,
        client,
        model: str,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]],
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> str:
        return self._chat_once_with_usage(
            client,
            model,
            messages,
            response_format,
            max_tokens,
            enable_thinking,
        ).content

    def _chat_once_with_usage(
        self,
        client,
        model: str,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]],
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> ProviderTextResult:
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            kwargs["max_tokens"] = max_tokens
        if self.provider != "openai" and isinstance(enable_thinking, bool):
            kwargs["extra_body"] = {"enable_thinking": enable_thinking}

        try:
            response = client.chat.completions.create(**kwargs)
            usage = getattr(response, "usage", None)
            if isinstance(usage, dict):
                input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
                output_tokens = usage.get(
                    "completion_tokens",
                    usage.get("output_tokens"),
                )
            else:
                input_tokens = getattr(
                    usage,
                    "prompt_tokens",
                    getattr(usage, "input_tokens", None),
                )
                output_tokens = getattr(
                    usage,
                    "completion_tokens",
                    getattr(usage, "output_tokens", None),
                )
            if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
                raise RuntimeError("供应商响应缺少 token 用量")
            request_id = getattr(response, "_request_id", None) or getattr(
                response,
                "id",
                None,
            )
            return ProviderTextResult(
                content=response.choices[0].message.content,
                raw_usage={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                provider_request_id=str(request_id) if request_id else None,
            )
        except Exception as e:
            provider_label = "DashScope" if self.provider != "openai" else "OpenAI"
            raise RuntimeError(f"{provider_label} API error: {e}") from e
