#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable, Mapping, Sequence


COMMON_PLATFORM_SECRET_NAMES = (
    "LUMENX_SESSION_SECRET",
    "LUMENX_OSS_ACCESS_KEY_SECRET",
    "DASHSCOPE_API_KEY",
    "ARK_API_KEY",
    "KLING_SECRET_KEY",
    "VIDU_API_KEY",
    "MULEROUTER_API_KEY",
)
FORBIDDEN_MARKERS = (
    b"LUMENX_DEPLOYMENT_MODE=cloud",
    b'"deployment_mode":"cloud"',
    b'"deployment_mode": "cloud"',
    b"NEXT_PUBLIC_DEPLOYMENT_MODE=cloud",
)


def configured_secret_values(environment: Mapping[str, str]) -> tuple[bytes, ...]:
    names = set(COMMON_PLATFORM_SECRET_NAMES)
    names.update(
        name.strip()
        for name in environment.get("LUMENX_PROVIDER_SECRET_REFS", "").split(",")
        if name.strip()
    )
    values: list[bytes] = []
    for name in sorted(names):
        value = environment.get(name, "").strip()
        if len(value) >= 8 and not value.lower().startswith(
            ("your_", "change-me", "replace-with")
        ):
            values.append(value.encode("utf-8"))
    return tuple(values)


def _artifact_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    yield from (candidate for candidate in path.rglob("*") if candidate.is_file())


def _contains(path: Path, needles: Sequence[bytes]) -> set[int]:
    if not needles:
        return set()
    matched: set[int] = set()
    overlap = max(len(needle) for needle in needles) - 1
    previous = b""
    with path.open("rb") as artifact_file:
        while chunk := artifact_file.read(1024 * 1024):
            data = previous + chunk
            matched.update(index for index, needle in enumerate(needles) if needle in data)
            if len(matched) == len(needles):
                return matched
            previous = data[-overlap:] if overlap > 0 else b""
    return matched


def scan_desktop_artifact(
    artifact: str | os.PathLike[str],
    *,
    secret_values: Sequence[bytes] = (),
) -> list[str]:
    root = Path(artifact)
    if not root.exists():
        return [f"桌面产物不存在：{root}"]

    findings: list[str] = []
    needles = (*FORBIDDEN_MARKERS, *secret_values)
    for candidate in _artifact_files(root):
        relative = candidate.relative_to(root) if root.is_dir() else candidate.name
        lowered_parts = [part.lower() for part in candidate.parts]
        if candidate.name == ".env" or candidate.name.lower().startswith(".env."):
            findings.append(f"包含环境配置文件：{relative}")
        if "secrets" in lowered_parts:
            findings.append(f"包含平台密钥目录：{relative}")
        try:
            matched = _contains(candidate, needles)
        except OSError as exc:
            findings.append(f"无法检查产物文件 {relative}：{exc}")
            continue
        for index in sorted(matched):
            if index < len(FORBIDDEN_MARKERS):
                marker = FORBIDDEN_MARKERS[index].decode("utf-8")
                findings.append(f"包含云端运行标记 {marker}：{relative}")
            else:
                findings.append(f"包含构建环境中的平台密钥字节：{relative}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 LumenX 桌面构建产物安全边界")
    parser.add_argument("artifact", help="待检查的 App、EXE 或产物目录")
    args = parser.parse_args()
    findings = scan_desktop_artifact(
        args.artifact,
        secret_values=configured_secret_values(os.environ),
    )
    if findings:
        print("桌面产物安全验证失败：")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("桌面产物安全验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
