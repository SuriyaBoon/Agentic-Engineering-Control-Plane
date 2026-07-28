from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .lifecycle import ExecutionResult, RemediationPlan, WorkItem, stable_id, utc_now
from .planning import ToolRegistry


class SafeExecutor:
    def __init__(self, runtime_root: str | Path, registry: ToolRegistry) -> None:
        self.runtime_root = Path(runtime_root)
        self.registry = registry

    def execute(
        self,
        *,
        item: WorkItem,
        plan: RemediationPlan,
        actor: str,
        attempt: int,
    ) -> ExecutionResult:
        tool = self.registry.get(plan.tool_name)
        if not tool.enabled:
            raise PermissionError(f"tool is disabled by policy: {tool.name}")
        if tool.side_effects:
            raise PermissionError("side-effecting tools are not available in safe execution")
        if plan.execution_mode not in {"dry_run", "mock"}:
            raise PermissionError(
                f"unsupported safe execution mode: {plan.execution_mode}"
            )
        if plan.source_repository_mutation:
            raise PermissionError("source mutation plans are prohibited")

        execution_id = stable_id(
            "EXEC",
            item.work_item_id,
            str(attempt),
            utc_now(),
        )
        artifact = {
            "schema_version": "1.0.0",
            "execution_id": execution_id,
            "work_item_id": item.work_item_id,
            "repository": item.repository,
            "source_commit": item.source_commit,
            "finding": {
                "finding_id": item.finding_id,
                "title": item.title,
                "category": item.category,
                "severity": item.severity,
                "evidence": item.evidence,
            },
            "plan": plan.to_dict(),
            "actor": actor,
            "attempt": attempt,
            "status": "simulated",
            "source_repository_mutation": False,
            "production_execution": False,
            "proposed_change": item.recommendation,
            "generated_at": utc_now(),
        }
        output = (
            self.runtime_root
            / "workflow"
            / "executions"
            / item.work_item_id
            / f"attempt-{attempt}.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        output.write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        return ExecutionResult(
            execution_id=execution_id,
            work_item_id=item.work_item_id,
            actor=actor,
            tool_name=tool.name,
            attempt=attempt,
            status="simulated",
            artifact_path=str(output.resolve()),
            artifact_sha256=digest,
            source_repository_mutation=False,
            details={
                "production_execution": False,
                "proposed_change": item.recommendation,
            },
        )


class ExecutionValidator:
    name = "validator"

    def validate(self, result: ExecutionResult) -> tuple[bool, list[str]]:
        failures: list[str] = []
        path = Path(result.artifact_path)
        if not path.is_file():
            failures.append("artifact_missing")
            return False, failures
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != result.artifact_sha256:
            failures.append("artifact_sha256_mismatch")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append("artifact_invalid_json")
            return False, failures
        if payload.get("source_repository_mutation") is not False:
            failures.append("source_repository_mutation_guard_failed")
        if payload.get("production_execution") is not False:
            failures.append("production_execution_guard_failed")
        if payload.get("status") != "simulated":
            failures.append("unexpected_execution_status")
        return not failures, failures
