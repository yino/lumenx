"""Batch continuity ledger for adjacent shots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core.contracts import FindingSeverity, ShotPackage, ValidationFinding, make_finding


@dataclass
class ContinuityLedger:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def check(self, shots: list[ShotPackage]) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        self.entries = []
        previous: ShotPackage | None = None
        for shot in shots:
            if previous is not None:
                self._compare(previous, shot, findings)
            entry = {
                "shot_id": shot.shot_id,
                "subject": shot.subject,
                "scene": shot.scene,
                "end_state": shot.end_state,
                "style": shot.style,
                "references": [reference.alias for reference in shot.references],
                "metadata": dict(shot.metadata),
            }
            self.entries.append(entry)
            previous = shot
        return findings

    def _compare(self, previous: ShotPackage, current: ShotPackage, findings: list[ValidationFinding]) -> None:
        intentional = current.metadata.get("intentional_changes") or []
        if previous.subject and current.subject and previous.subject != current.subject and "subject" not in intentional:
            findings.append(make_finding(FindingSeverity.WARNING, "continuity_subject", "相邻镜头主体描述发生变化", "continuity-validator", shot_id=current.shot_id))
        for key, label in (("costume", "服装"), ("lighting", "光线"), ("axis", "轴线"), ("prop_state", "道具状态")):
            old = previous.metadata.get(key)
            new = current.metadata.get(key)
            if old is not None and new is not None and old != new and key not in intentional:
                findings.append(make_finding(FindingSeverity.WARNING, f"continuity_{key}", f"相邻镜头{label}状态未说明变化", "continuity-validator", shot_id=current.shot_id, details={"previous": old, "current": new}))
