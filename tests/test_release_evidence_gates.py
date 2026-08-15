from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts.legacy_api_compat_evidence import (
    CompatibilityEvidenceFailure,
    summarize_logs,
    validate_client_inventory,
    validate_metric_snapshots,
)
from scripts.staging_cloud_canary import (
    CanaryFailure,
    _require_matching_runtime_fingerprints,
    _require_closed_state,
    _require_execute_state,
    _validate_origin,
    _validate_staging_identity,
    validate_isolation_manifest,
)


def _config(*, registration_mode: str, new_ai_tasks_enabled: bool) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "platform": {
            "feature_flags": {
                "registration_mode": registration_mode,
                "new_ai_tasks_enabled": new_ai_tasks_enabled,
            },
            "exposed_capabilities": ["image.t2i"],
        },
        "routes": [
            {
                "capability": "image.t2i",
                "enabled": True,
                "is_primary": True,
                "provider_model_id": "wan2.5-t2i-preview",
            }
        ],
    }


def test_staging_canary_requires_explicit_nonproduction_https_target() -> None:
    assert (
        _validate_staging_identity("lumenx-staging", "lumenx-staging")
        == "lumenx-staging"
    )
    assert _validate_origin("https://staging.example.invalid/") == (
        "https://staging.example.invalid"
    )

    with pytest.raises(CanaryFailure, match="生产"):
        _validate_staging_identity("lumenx-production", "lumenx-production")
    with pytest.raises(CanaryFailure, match="完全一致"):
        _validate_staging_identity("lumenx-staging", "other-staging")
    with pytest.raises(CanaryFailure, match="HTTPS"):
        _validate_origin("http://staging.example.invalid")


def test_staging_canary_state_machine_never_opens_registration() -> None:
    closed_deployment = {
        "registration_emergency_disabled": True,
        "new_ai_tasks_emergency_disabled": True,
    }
    execute_deployment = {
        "registration_emergency_disabled": True,
        "new_ai_tasks_emergency_disabled": False,
    }
    _require_closed_state(
        closed_deployment,
        _config(registration_mode="disabled", new_ai_tasks_enabled=False),
    )
    _require_execute_state(
        execute_deployment,
        _config(registration_mode="disabled", new_ai_tasks_enabled=True),
        "image.t2i",
        "wan2.5-t2i-preview",
    )

    with pytest.raises(CanaryFailure, match="注册"):
        _require_execute_state(
            execute_deployment,
            _config(registration_mode="invite_only", new_ai_tasks_enabled=True),
            "image.t2i",
            "wan2.5-t2i-preview",
        )


def test_staging_canary_requires_recent_distinct_resource_fingerprints(
    tmp_path,
) -> None:
    manifest = tmp_path / "isolation.json"
    resources = {
        name: {
            "staging_fingerprint": f"sha256:{index:064x}",
            "production_fingerprint": f"sha256:{index + 10:064x}",
        }
        for index, name in enumerate(
            ("postgresql", "redis", "oss_bucket", "provider_account"),
            start=1,
        )
    }
    payload = {
        "environment": "lumenx-staging",
        "reviewed_by": "release-owner",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "resources": resources,
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    observed = {
        name: resource["staging_fingerprint"]
        for name, resource in resources.items()
    }
    assert len(
        validate_isolation_manifest(manifest, "lumenx-staging", observed)
    ) == 64

    mismatched_observed = {**observed, "redis": f"sha256:{99:064x}"}
    with pytest.raises(CanaryFailure, match="当前部署不一致"):
        validate_isolation_manifest(
            manifest,
            "lumenx-staging",
            mismatched_observed,
        )

    with pytest.raises(CanaryFailure, match="当前部署缺少 provider_account"):
        validate_isolation_manifest(
            manifest,
            "lumenx-staging",
            {**observed, "provider_account": None},
        )

    resources["oss_bucket"]["production_fingerprint"] = resources["oss_bucket"][
        "staging_fingerprint"
    ]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CanaryFailure, match="未隔离"):
        validate_isolation_manifest(manifest, "lumenx-staging", observed)


def test_staging_canary_requires_local_runtime_to_match_edge_resources() -> None:
    fingerprints = {
        name: f"sha256:{index:064x}"
        for index, name in enumerate(
            ("postgresql", "redis", "oss_bucket", "provider_account"),
            start=1,
        )
    }

    _require_matching_runtime_fingerprints(fingerprints, fingerprints)

    with pytest.raises(CanaryFailure, match="本地运行环境与目标部署"):
        _require_matching_runtime_fingerprints(
            fingerprints,
            {**fingerprints, "postgresql": f"sha256:{99:064x}"},
        )


def test_legacy_compatibility_evidence_counts_only_window_requests(tmp_path) -> None:
    log = tmp_path / "lumenx-legacy-api.log"
    log.write_text(
        '127.0.0.1 - - [01/Aug/2026:00:00:00 +0000] "GET /auth/me HTTP/1.1" 200 2\n'
        '127.0.0.1 - - [10/Aug/2026:12:00:00 +0000] "GET /wallet HTTP/1.1" 200 2\n',
        encoding="utf-8",
    )

    summaries = summarize_logs(
        [log],
        datetime(2026, 8, 8, tzinfo=UTC),
        datetime(2026, 8, 15, tzinfo=UTC),
    )

    assert len(summaries) == 1
    assert summaries[0].parsed_lines == 2
    assert summaries[0].requests_in_window == 1
    assert len(summaries[0].sha256) == 64


def test_legacy_compatibility_gate_requires_v1_client_inventory_and_zero_metrics(
    tmp_path,
) -> None:
    start = datetime(2026, 8, 8, tzinfo=UTC)
    end = datetime(2026, 8, 15, tzinfo=UTC)
    inventory = tmp_path / "clients.json"
    inventory.write_text(
        json.dumps(
            {
                "environment": "production-cn",
                "supported_cloud_clients": [
                    {
                        "name": "hosted-web",
                        "owner": "frontend-team",
                        "api_contract": "/api/v1",
                        "validated_at": "2026-08-14T12:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshots = []
    for index, generated_at in enumerate((start.timestamp(), end.timestamp()), start=1):
        snapshot = tmp_path / f"metrics-{index}.json"
        snapshot.write_text(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "counters": [],
                    "gauges": [],
                    "histograms": [],
                }
            ),
            encoding="utf-8",
        )
        snapshots.append(snapshot)

    client_count, inventory_hash = validate_client_inventory(
        inventory,
        "production-cn",
        start,
        end,
    )
    metric_count, metric_hash, metric_delta = validate_metric_snapshots(
        snapshots,
        start,
        end,
    )

    assert client_count == 1
    assert len(inventory_hash) == 64
    assert metric_count == 2
    assert len(metric_hash) == 64
    assert metric_delta == 0

    payload = json.loads(snapshots[1].read_text(encoding="utf-8"))
    payload["counters"] = [
        {
            "name": "cloud_legacy_api_requests_total",
            "labels": {"operation": "edge_compatibility"},
            "value": 1,
        }
    ]
    snapshots[1].write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CompatibilityEvidenceFailure, match="仍有旧路由"):
        validate_metric_snapshots(snapshots, start, end)
