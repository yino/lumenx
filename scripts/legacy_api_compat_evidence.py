#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, TextIO


LOG_TIMESTAMP = re.compile(r"\[(?P<timestamp>[^\]]+)\]")
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$")


class CompatibilityEvidenceFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LogSummary:
    path: str
    sha256: str
    parsed_lines: int
    requests_in_window: int


def _safe_label(value: str, name: str) -> str:
    normalized = value.strip()
    if not SAFE_LABEL.fullmatch(normalized):
        raise CompatibilityEvidenceFailure(
            f"{name}只能包含字母、数字、点、下划线和连字符"
        )
    return normalized


def _parse_iso(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompatibilityEvidenceFailure(f"{name}必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise CompatibilityEvidenceFailure(f"{name}必须包含时区")
    return parsed.astimezone(UTC)


def _open_log(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8", errors="strict")
    return path.open(mode="rt", encoding="utf-8", errors="strict")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_nginx_timestamp(line: str) -> datetime:
    match = LOG_TIMESTAMP.search(line)
    if match is None:
        raise CompatibilityEvidenceFailure("兼容访问日志包含无法解析的行")
    try:
        parsed = datetime.strptime(
            match.group("timestamp"),
            "%d/%b/%Y:%H:%M:%S %z",
        )
    except ValueError as exc:
        raise CompatibilityEvidenceFailure("兼容访问日志时间格式无效") from exc
    return parsed.astimezone(UTC)


def summarize_logs(
    paths: Iterable[Path],
    start: datetime,
    end: datetime,
) -> list[LogSummary]:
    summaries: list[LogSummary] = []
    for path in paths:
        if not path.is_file():
            raise CompatibilityEvidenceFailure(f"兼容访问日志不存在：{path}")
        parsed_lines = 0
        requests_in_window = 0
        try:
            with _open_log(path) as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    occurred_at = _parse_nginx_timestamp(line)
                    parsed_lines += 1
                    if start <= occurred_at <= end:
                        requests_in_window += 1
        except UnicodeError as exc:
            raise CompatibilityEvidenceFailure("兼容访问日志不是有效 UTF-8") from exc
        summaries.append(
            LogSummary(
                path=path.name,
                sha256=_sha256(path),
                parsed_lines=parsed_lines,
                requests_in_window=requests_in_window,
            )
        )
    if not summaries:
        raise CompatibilityEvidenceFailure("至少需要一个兼容访问日志文件")
    return summaries


def validate_client_inventory(
    path: Path,
    environment: str,
    start: datetime,
    end: datetime,
) -> tuple[int, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompatibilityEvidenceFailure("无法读取受支持客户端清单") from exc
    if not isinstance(payload, dict) or payload.get("environment") != environment:
        raise CompatibilityEvidenceFailure("客户端清单环境与观察环境不一致")
    clients = payload.get("supported_cloud_clients")
    if not isinstance(clients, list) or not clients:
        raise CompatibilityEvidenceFailure(
            "客户端清单至少需要一个受支持云端客户端"
        )
    names: set[str] = set()
    for client in clients:
        if not isinstance(client, dict):
            raise CompatibilityEvidenceFailure("客户端清单条目格式无效")
        name = _safe_label(str(client.get("name") or ""), "客户端名")
        if name in names:
            raise CompatibilityEvidenceFailure("客户端清单包含重复客户端")
        names.add(name)
        if client.get("api_contract") != "/api/v1":
            raise CompatibilityEvidenceFailure(
                f"受支持客户端 {name} 尚未声明使用 /api/v1"
            )
        owner = str(client.get("owner") or "").strip()
        if not owner or len(owner) > 120:
            raise CompatibilityEvidenceFailure(f"受支持客户端 {name} 缺少责任人")
        validated_at = _parse_iso(str(client.get("validated_at") or ""), "验证时间")
        if validated_at < start or validated_at > end:
            raise CompatibilityEvidenceFailure(
                f"受支持客户端 {name} 未在观察窗口内完成验证"
            )
    return len(clients), _sha256(path)


def _counter_value(payload: dict[str, Any]) -> float:
    total = 0.0
    counters = payload.get("counters")
    if not isinstance(counters, list):
        raise CompatibilityEvidenceFailure("指标快照缺少 counters")
    for row in counters:
        if not isinstance(row, dict):
            raise CompatibilityEvidenceFailure("指标快照计数条目格式无效")
        if row.get("name") != "cloud_legacy_api_requests_total":
            continue
        labels = row.get("labels")
        if labels != {"operation": "edge_compatibility"}:
            raise CompatibilityEvidenceFailure("旧路由指标包含非预期标签")
        value = row.get("value")
        if not isinstance(value, (int, float)) or value < 0:
            raise CompatibilityEvidenceFailure("旧路由指标值无效")
        total += float(value)
    return total


def validate_metric_snapshots(
    paths: Iterable[Path],
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[int, str, float]:
    count = 0
    combined_hash = hashlib.sha256()
    observations: list[tuple[datetime, float]] = []
    for path in paths:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise CompatibilityEvidenceFailure("无法读取旧路由指标快照") from exc
        if not isinstance(payload, dict):
            raise CompatibilityEvidenceFailure("旧路由指标快照格式无效")
        counter = _counter_value(payload)
        generated_at = payload.get("generated_at")
        if not isinstance(generated_at, (int, float)) or generated_at <= 0:
            raise CompatibilityEvidenceFailure("指标快照缺少生成时间")
        observations.append((datetime.fromtimestamp(generated_at, UTC), counter))
        combined_hash.update(hashlib.sha256(raw).digest())
        count += 1
    if count < 2:
        raise CompatibilityEvidenceFailure("至少需要观察窗口起止两个指标快照")
    observations.sort(key=lambda item: item[0])
    for previous, current in zip(observations, observations[1:]):
        if current[1] < previous[1]:
            raise CompatibilityEvidenceFailure("观察窗口内旧路由计数器发生重置")
    if start is not None and observations[0][0] > start:
        raise CompatibilityEvidenceFailure("首个指标快照晚于观察窗口开始")
    if end is not None and observations[-1][0] < end:
        raise CompatibilityEvidenceFailure("最后指标快照早于观察窗口结束")
    delta = observations[-1][1] - observations[0][1]
    if delta != 0:
        raise CompatibilityEvidenceFailure("指标快照显示观察窗口内仍有旧路由请求")
    return count, combined_hash.hexdigest(), delta


def _write_evidence(
    path: Path,
    *,
    environment: str,
    start: datetime,
    end: datetime,
    minimum_hours: int,
    client_count: int,
    inventory_hash: str,
    logs: list[LogSummary],
    metric_count: int,
    metric_hash: str,
    metric_delta: float,
    passed: bool,
    reason: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    requests_in_window = sum(item.requests_in_window for item in logs)
    log_rows = [
        f"- 日志 `{item.path}`：SHA-256 `{item.sha256}`，"
        f"窗口内旧请求 {item.requests_in_window}"
        for item in logs
    ]
    decision = (
        "GO：可以在下一次受控发布中把 `LUMENX_LEGACY_API_COMPAT` 设为 `false`，"
        "发布后继续观察 404 和客户端错误率。"
        if passed
        else f"NO-GO：{reason}。保持兼容路由开启并继续观察。"
    )
    lines = [
        "# LumenX 旧云端 API 兼容窗口证据",
        "",
        f"- 环境：`{environment}`",
        f"- 观察开始：`{start.isoformat()}`",
        f"- 观察结束：`{end.isoformat()}`",
        f"- 最短观察时长：{minimum_hours} 小时",
        f"- 实际观察时长：{(end - start).total_seconds() / 3600:.2f} 小时",
        f"- 受支持云端客户端：{client_count}",
        f"- 客户端清单 SHA-256：`{inventory_hash}`",
        f"- 指标快照数：{metric_count}",
        f"- 指标快照组合 SHA-256：`{metric_hash}`",
        f"- 观察窗口旧路由指标增量：{metric_delta:g}",
        f"- 窗口内旧路由请求总数：{requests_in_window}",
        "",
        "## 日志证据",
        "",
        *log_rows,
        "",
        "## 结论",
        "",
        decision,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="根据完整访问窗口生成旧云端 API 兼容路由退出证据",
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--minimum-window-hours", type=int, default=168)
    parser.add_argument("--access-log", action="append", type=Path, required=True)
    parser.add_argument("--metrics-snapshot", action="append", type=Path, required=True)
    parser.add_argument("--supported-clients", type=Path, required=True)
    parser.add_argument("--confirm-complete-log-window", required=True)
    parser.add_argument("--evidence-path", type=Path, required=True)
    args = parser.parse_args()

    try:
        environment = _safe_label(args.environment, "环境名")
        start = _parse_iso(args.window_start, "观察开始时间")
        end = _parse_iso(args.window_end, "观察结束时间")
        if end <= start:
            raise CompatibilityEvidenceFailure("观察结束时间必须晚于开始时间")
        if end > datetime.now(UTC):
            raise CompatibilityEvidenceFailure("观察结束时间不能位于未来")
        if args.minimum_window_hours < 24:
            raise CompatibilityEvidenceFailure("最短观察窗口不能少于 24 小时")
        duration_hours = (end - start).total_seconds() / 3600
        if duration_hours < args.minimum_window_hours:
            raise CompatibilityEvidenceFailure("观察窗口时长不足")
        expected_confirmation = f"{environment}|{start.isoformat()}|{end.isoformat()}"
        if args.confirm_complete_log_window != expected_confirmation:
            raise CompatibilityEvidenceFailure(
                "完整日志窗口确认值不匹配；"
                "应使用规范化环境与 UTC 起止时间"
            )
        logs = summarize_logs(args.access_log, start, end)
        client_count, inventory_hash = validate_client_inventory(
            args.supported_clients,
            environment,
            start,
            end,
        )
        metric_count, metric_hash, metric_delta = validate_metric_snapshots(
            args.metrics_snapshot,
            start,
            end,
        )
        request_count = sum(item.requests_in_window for item in logs)
        passed = request_count == 0
        reason = "观察窗口内仍有旧路由请求" if request_count else "全部门禁通过"
        _write_evidence(
            args.evidence_path,
            environment=environment,
            start=start,
            end=end,
            minimum_hours=args.minimum_window_hours,
            client_count=client_count,
            inventory_hash=inventory_hash,
            logs=logs,
            metric_count=metric_count,
            metric_hash=metric_hash,
            metric_delta=metric_delta,
            passed=passed,
            reason=reason,
        )
        if not passed:
            print(f"旧路由兼容窗口未通过：{reason}")
            return 2
        print(f"旧路由兼容窗口通过：{args.evidence_path}")
        return 0
    except CompatibilityEvidenceFailure as exc:
        print(f"旧路由兼容窗口未通过：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
