from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


WORK_STATES = {
    "new",
    "triaged",
    "planned",
    "awaiting_execution_approval",
    "approved",
    "executing",
    "validating",
    "awaiting_closure_approval",
    "closed",
    "manual_review",
    "failed",
}

TERMINAL_STATES = {"closed", "failed"}

ALLOWED_TRANSITIONS = {
    "new": {"triaged"},
    "triaged": {"planned", "manual_review"},
    "planned": {"awaiting_execution_approval", "manual_review"},
    "awaiting_execution_approval": {"approved", "manual_review"},
    "approved": {"executing", "manual_review"},
    "executing": {"validating", "approved", "manual_review", "failed"},
    "validating": {"awaiting_closure_approval", "approved", "manual_review", "failed"},
    "awaiting_closure_approval": {"closed", "manual_review"},
    "manual_review": {"triaged", "planned", "approved", "failed"},
    "closed": set(),
    "failed": {"triaged"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: str) -> str:
    material = "|".join(parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


@dataclass
class WorkItem:
    work_item_id: str
    finding_id: str
    repository: str
    source_commit: str | None
    source_run_id: str
    title: str
    category: str
    severity: str
    recommendation: str
    evidence: list[str]
    state: str = "new"
    owner: str | None = None
    created_by: str = "audit-ingest"
    plan_id: str | None = None
    due_at: str | None = None
    retry_count: int = 0
    max_retries: int = 2
    last_error: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.state not in WORK_STATES:
            raise ValueError(f"unsupported work state: {self.state}")
        if not self.evidence:
            raise ValueError("work item evidence is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemediationPlan:
    plan_id: str
    work_item_id: str
    planner: str
    tool_name: str
    execution_mode: str
    steps: tuple[str, ...]
    validation_checks: tuple[str, ...]
    fallback: str
    max_retries: int
    source_repository_mutation: bool = False
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = list(self.steps)
        payload["validation_checks"] = list(self.validation_checks)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RemediationPlan":
        return cls(
            plan_id=str(payload["plan_id"]),
            work_item_id=str(payload["work_item_id"]),
            planner=str(payload["planner"]),
            tool_name=str(payload["tool_name"]),
            execution_mode=str(payload["execution_mode"]),
            steps=tuple(str(item) for item in payload["steps"]),
            validation_checks=tuple(
                str(item) for item in payload["validation_checks"]
            ),
            fallback=str(payload["fallback"]),
            max_retries=int(payload["max_retries"]),
            source_repository_mutation=bool(
                payload.get("source_repository_mutation", False)
            ),
            created_at=str(payload["created_at"]),
        )


@dataclass(frozen=True)
class Approval:
    work_item_id: str
    stage: str
    actor: str
    decision: str
    comment: str
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.stage not in {"execution", "closure"}:
            raise ValueError(f"unsupported approval stage: {self.stage}")
        if self.decision not in {"approved", "rejected"}:
            raise ValueError(f"unsupported approval decision: {self.decision}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    work_item_id: str
    actor: str
    tool_name: str
    attempt: int
    status: str
    artifact_path: str
    artifact_sha256: str
    source_repository_mutation: bool
    details: dict[str, Any]
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
