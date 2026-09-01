from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_frontend_can_run_from_a_prebuilt_image() -> None:
    compose = (ROOT / "docker-compose-service.yml").read_text(encoding="utf-8")
    prebuilt = (ROOT / "docker-compose-service-prebuilt.yml").read_text(
        encoding="utf-8"
    )

    assert "image: ${LUMENX_FRONTEND_IMAGE:-lumenx-frontend:local}" in compose
    assert "pull_policy: never" in compose
    assert "image: ${LUMENX_FRONTEND_IMAGE:?" in prebuilt
    assert "build: !reset null" in prebuilt
    assert "pull_policy: never" in prebuilt


def test_frontend_image_builder_targets_linux_by_default_and_can_export() -> None:
    script = (ROOT / "scripts/build-frontend-image.sh").read_text(encoding="utf-8")

    assert 'platform="${LUMENX_FRONTEND_PLATFORM:-linux/amd64}"' in script
    assert 'image="${LUMENX_FRONTEND_IMAGE:-lumenx-frontend:local}"' in script
    assert 'archive="${LUMENX_FRONTEND_ARCHIVE:-}"' in script
    assert "docker buildx build" in script
    assert "--load" in script
    assert "docker save \"$image\" | gzip -c" in script


def test_prebuilt_compose_merge_removes_frontend_build_definition() -> None:
    """Keep the deployment contract explicit for Compose's !reset merge tag."""

    prebuilt = (ROOT / "docker-compose-service-prebuilt.yml").read_text(
        encoding="utf-8"
    )
    frontend_block = prebuilt.split("  frontend:", 1)[1]

    assert "build: !reset null" in frontend_block
    assert "image: ${LUMENX_FRONTEND_IMAGE:?" in frontend_block
