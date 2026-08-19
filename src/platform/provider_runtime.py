from __future__ import annotations

import json
import mimetypes
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from src.apps.comic_gen.llm import (
    DEFAULT_ENTITY_EXTRACTION_PROMPT,
    DEFAULT_R2V_POLISH_PROMPT,
    DEFAULT_STORYBOARD_EXTRACTION_PROMPT,
    DEFAULT_STYLE_ANALYSIS_PROMPT,
    DEFAULT_VIDEO_POLISH_PROMPT,
)

from .ai_io import DownloadedProviderOutput, ProviderOutputReference
from .ai_worker import ProviderInvocationOutcome
from .settings import DeploymentMode, DeploymentSettings, ProviderAdapter
from .video_providers import VideoGenerationRequest


class ProductionProviderInvoker:
    """Bridges immutable cloud task snapshots to the existing provider adapters."""

    def __init__(self, settings: DeploymentSettings) -> None:
        if (
            settings.deployment_mode is not DeploymentMode.CLOUD
            or settings.provider_adapter is not ProviderAdapter.PRODUCTION
        ):
            raise RuntimeError("生产供应商执行器只能在正常云端模式中启用")
        self.output_root = settings.provider_output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _content(task) -> str:
        content = task.request_payload.get("content")
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _polish_user_text(content: Mapping[str, Any]) -> str:
        draft_prompt = str(content.get("draft_prompt") or "")
        feedback = str(content.get("feedback") or "").strip()
        prev_cn = str(content.get("prev_cn") or "").strip()
        if not feedback:
            return draft_prompt
        if prev_cn:
            return f"""[当前提示词-CN]
{prev_cn}

[当前提示词-EN]
{draft_prompt}

[用户反馈]
{feedback}

请根据用户反馈同步修改双语版本，只修改用户指出的问题，保持其他部分不变。"""
        return f"""[当前提示词]
{draft_prompt}

[用户反馈]
{feedback}

请根据用户反馈修改提示词，只修改用户指出的问题，保持其他部分不变。"""

    @staticmethod
    def _polish_user_content(task, text: str) -> str | list[dict[str, Any]]:
        image_urls = [
            str(provider_input.signed_url)
            for provider_input in getattr(task, "provider_inputs", ())
            if getattr(provider_input, "signed_url", None)
        ]
        if not image_urls:
            return text
        return [
            *[
                {"type": "image_url", "image_url": {"url": image_url}}
                for image_url in image_urls
            ],
            {"type": "text", "text": text},
        ]

    @staticmethod
    def _text_request(task) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        content = task.request_payload.get("content")
        if not isinstance(content, Mapping):
            return ([{"role": "user", "content": ProductionProviderInvoker._content(task)}], None)

        operation = str(content.get("operation") or "")
        if operation in {
            "project.create_and_analyze",
            "project.extract_preview",
            "project.reparse",
        }:
            text = str(content.get("text") or "").strip()
            prompt = DEFAULT_ENTITY_EXTRACTION_PROMPT.replace("{text}", text)
            return ([{"role": "user", "content": prompt}], {"type": "json_object"})

        if operation == "art_direction.analyze":
            script_text = str(content.get("script_text") or "").strip()
            return (
                [
                    {"role": "system", "content": DEFAULT_STYLE_ANALYSIS_PROMPT},
                    {"role": "user", "content": f"剧本内容：\n\n{script_text[:12000]}"},
                ],
                {"type": "json_object"},
            )

        if operation in {"storyboard.analyze", "storyboard.generate"}:
            text = str(content.get("text") or "").strip()
            entities = content.get("entities")
            if not isinstance(entities, Mapping):
                entities = {"characters": [], "scenes": [], "props": []}
            entities_text = json.dumps(entities, ensure_ascii=False, indent=2)
            prompt = DEFAULT_STORYBOARD_EXTRACTION_PROMPT.replace(
                "{entities_str}", entities_text
            ).replace("{text}", text)
            return (
                [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": "请生成覆盖剧本内容的分镜帧，并严格返回合法 JSON。",
                    },
                ],
                {"type": "json_object"},
            )

        if operation in {"video.prompt.polish", "video.r2v_prompt.polish"}:
            user_text = ProductionProviderInvoker._polish_user_text(content)
            user_content = ProductionProviderInvoker._polish_user_content(
                task,
                user_text,
            )
            has_images = isinstance(user_content, list)
            if operation == "video.r2v_prompt.polish":
                raw_slots = content.get("slots")
                slots = raw_slots if isinstance(raw_slots, list) else []
                slot_context = [
                    f"- character{index}: {slot.get('description', '')}"
                    for index, slot in enumerate(slots, start=1)
                    if isinstance(slot, Mapping)
                ]
                system_prompt = DEFAULT_R2V_POLISH_PROMPT.replace(
                    "{SLOTS}",
                    "\n".join(slot_context) or "No reference videos provided.",
                )
                if has_images:
                    system_prompt += (
                        "\n\nIMPORTANT: The user has attached the reference image(s) "
                        "for character1/2/3. Use the visible appearance, costume, and pose "
                        "to ground the action and camera description. Do not contradict "
                        "visible details."
                    )
            else:
                system_prompt = DEFAULT_VIDEO_POLISH_PROMPT
                if has_images:
                    system_prompt += (
                        "\n\nIMPORTANT: The user has attached the first frame image(s) "
                        "of the clip. Use the visible subjects, composition, lighting, and "
                        "color palette to ground the motion and camera description. Do not "
                        "invent elements absent from the image."
                    )
            return (
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                {"type": "json_object"},
            )

        return ([{"role": "user", "content": ProductionProviderInvoker._content(task)}], None)

    def _output_path(self, task, suffix: str, *, index: int | None = None) -> Path:
        stem = task.attempt_id if index is None else f"{task.attempt_id}-{index}"
        path = (self.output_root / task.task_id / f"{stem}{suffix}").resolve()
        if not path.is_relative_to(self.output_root):
            raise RuntimeError("供应商临时输出路径越界")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _generation_prompt(task) -> str:
        content = task.request_payload.get("content")
        if isinstance(content, Mapping):
            prompt = content.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                style_prompt = content.get("style_prompt")
                if content.get("apply_style") and isinstance(style_prompt, str):
                    return ", ".join(
                        part.strip() for part in (prompt, style_prompt) if part.strip()
                    )
                return prompt.strip()
        return ProductionProviderInvoker._content(task)

    @staticmethod
    def _speech_items(task) -> tuple[str, list[dict[str, Any]]]:
        content = task.request_payload.get("content")
        if not isinstance(content, Mapping):
            raise RuntimeError("语音任务缺少结构化内容")
        operation = str(content.get("operation") or "speech.tts")
        raw_items = content.get("items")
        if raw_items is None:
            raw_items = [content]
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 100:
            raise RuntimeError("语音任务条目数量无效")

        items: list[dict[str, Any]] = []
        total_characters = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise RuntimeError("语音任务条目格式无效")
            text = str(raw_item.get("text") or "").strip()
            voice_id = str(raw_item.get("voice_id") or "").strip()
            if not text or not voice_id:
                raise RuntimeError("语音任务缺少台词或音色")
            total_characters += len(text)
            items.append({**dict(raw_item), "text": text, "voice_id": voice_id})

        maximum = int(task.model_route.metering_formula.get("max_units", 0))
        if maximum <= 0 or total_characters > maximum:
            raise RuntimeError("语音任务字符数超出模型配置上限")
        return operation, items

    @staticmethod
    def _audio_type(path: Path) -> tuple[Path, str]:
        header = path.read_bytes()[:12]
        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            suffix, content_type = ".wav", "audio/wav"
        elif header.startswith(b"OggS"):
            suffix, content_type = ".ogg", "audio/ogg"
        elif header.startswith(b"ID3") or header.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
            suffix, content_type = ".mp3", "audio/mpeg"
        else:
            raise RuntimeError("语音供应商返回了无法识别的音频格式")
        if path.suffix != suffix:
            destination = path.with_suffix(suffix)
            path.replace(destination)
            path = destination
        return path, content_type

    def _invoke_speech(self, client, task, on_provider_submission) -> ProviderInvocationOutcome:
        operation, items = self._speech_items(task)
        outputs: list[dict[str, str]] = []
        total_characters = 0
        for index, item in enumerate(items):
            output_path = self._output_path(task, ".mp3", index=index)
            generated_path, _delay, request_id = client.adapter.synthesize(
                item["text"],
                str(output_path),
                voice=item["voice_id"],
                speech_rate=float(item.get("speed", 1.0)),
                pitch_rate=float(item.get("pitch", 1.0)),
                volume=int(item.get("volume", 50)),
                instructions=(str(item["instructions"]) if item.get("instructions") else None),
                model_override=(str(item["model_override"]) if item.get("model_override") else None),
                family_override=(str(item["family_override"]) if item.get("family_override") else None),
            )
            if not getattr(on_provider_submission, "recorded", False):
                on_provider_submission(task.model_route.provider, None, request_id or None)
            generated = Path(generated_path).resolve()
            if not generated.is_relative_to(self.output_root) or not generated.is_file():
                raise RuntimeError("语音供应商输出未写入受控临时目录")
            generated, content_type = self._audio_type(generated)
            relative = generated.relative_to(self.output_root).as_posix()
            outputs.append(
                {
                    "url": f"lumenx-worker:///{quote(relative, safe='/')}",
                    "filename": generated.name,
                    "content_type": content_type,
                }
            )
            total_characters += len(item["text"])
        return ProviderInvocationOutcome(
            raw_usage={"characters": total_characters},
            result={
                "outputs": outputs,
                "content": {"operation": operation, "output_count": len(outputs)},
            },
        )

    def invoke(self, client, task, on_provider_submission) -> ProviderInvocationOutcome:
        capability = task.capability
        prompt = self._generation_prompt(task)
        if capability == "speech.tts":
            return self._invoke_speech(client, task, on_provider_submission)
        if capability in {"script.analysis", "prompt.polish"}:
            messages, response_format = self._text_request(task)
            max_output_tokens = task.model_route.metering_formula.get("max_output_tokens")
            result = client.adapter.chat_with_usage(
                messages,
                model=task.model_route.provider_model_id,
                response_format=response_format,
                max_tokens=(
                    max_output_tokens
                    if isinstance(max_output_tokens, int)
                    and not isinstance(max_output_tokens, bool)
                    and max_output_tokens > 0
                    else None
                ),
                enable_thinking=False,
            )
            on_provider_submission(
                task.model_route.provider,
                None,
                result.provider_request_id,
            )
            return ProviderInvocationOutcome(
                raw_usage=dict(result.raw_usage),
                result={"content": result.content},
            )

        if capability.startswith("video."):
            output_path = self._output_path(task, ".mp4")
            parameters = client.execution_parameters()
            parameters.pop("model", None)
            mode = capability.removeprefix("video.")
            generated = client.adapter.generate(
                VideoGenerationRequest(
                    model_id=task.model_route.provider_model_id,
                    prompt=prompt,
                    output_path=str(output_path),
                    mode=mode,
                    input_urls=tuple(
                        item.signed_url for item in task.provider_inputs
                    ),
                    parameters=parameters,
                    on_provider_submission=on_provider_submission,
                )
            )
            return self._generation_outcome(
                generated,
                content_type="video/mp4",
                on_provider_submission=on_provider_submission,
                provider=task.model_route.provider,
            )

        if capability.startswith("image."):
            suffix = ".png"
            content_type = "image/png"
        else:
            raise RuntimeError(f"云端供应商执行器暂不支持能力 {capability}")

        output_path = self._output_path(task, suffix)
        parameters = client.execution_parameters()
        parameters["on_provider_ids"] = on_provider_submission
        input_urls = [item.signed_url for item in task.provider_inputs]
        if capability.startswith("image."):
            parameters["n"] = int(
                parameters.pop("count", parameters.pop("output_count", 1))
            )
            if input_urls:
                parameters["ref_image_paths"] = input_urls

        generated = client.adapter.generate_with_usage(
            prompt,
            str(output_path),
            **parameters,
        )
        return self._generation_outcome(
            generated,
            content_type=content_type,
            on_provider_submission=on_provider_submission,
            provider=task.model_route.provider,
        )

    def _generation_outcome(
        self,
        generated,
        *,
        content_type: str,
        on_provider_submission,
        provider: str,
    ) -> ProviderInvocationOutcome:
        generated_path = Path(generated.output_path).resolve()
        if not generated_path.is_relative_to(self.output_root) or not generated_path.is_file():
            raise RuntimeError("供应商输出未写入受控临时目录")
        if not getattr(on_provider_submission, "recorded", False):
            on_provider_submission(provider, None, None)
        relative = generated_path.relative_to(self.output_root).as_posix()
        return ProviderInvocationOutcome(
            raw_usage=dict(generated.raw_usage),
            result={
                "outputs": [
                    {
                        "url": f"lumenx-worker:///{quote(relative, safe='/')}",
                        "filename": generated_path.name,
                        "content_type": content_type,
                    }
                ]
            },
        )


class ProductionProviderOutputDownloader:
    def __init__(self, settings: DeploymentSettings) -> None:
        if settings.deployment_mode is not DeploymentMode.CLOUD:
            raise RuntimeError("生产输出读取器只能在正常云端模式中启用")
        self.output_root = settings.provider_output_root.resolve()

    def download(
        self,
        provider: str,
        reference: ProviderOutputReference,
    ) -> DownloadedProviderOutput:
        del provider
        parsed = urlparse(reference.url)
        if parsed.scheme != "lumenx-worker" or parsed.netloc:
            raise RuntimeError("供应商临时输出引用无效")
        relative = unquote(parsed.path).lstrip("/")
        path = (self.output_root / relative).resolve()
        if not path.is_relative_to(self.output_root) or not path.is_file():
            raise RuntimeError("供应商临时输出不存在")
        content_type = (
            reference.declared_content_type
            or mimetypes.guess_type(reference.filename)[0]
            or "application/octet-stream"
        )
        return DownloadedProviderOutput(
            content=path.read_bytes(),
            content_type=content_type,
            filename=reference.filename,
        )
