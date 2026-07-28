from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .execution import ExecutionValidator, SafeExecutor
from .lifecycle import Approval, WorkItem, stable_id
from .planning import RemediationPlanner, ToolRegistry
from .policy import AuditPolicy
from .store import WorkflowStore


class GovernedWorkflow:
    def __init__(
        self,
        *,
        policy: AuditPolicy,
        runtime_root: str | Path,
        store: WorkflowStore | None = None,
    ) -> None:
        self.policy = policy
        self.runtime_root = Path(runtime_root)
        self.store = store or WorkflowStore(
            self.runtime_root / "workflow" / "workflow.db"
        )
        self.registry = ToolRegistry()
        self.planner = RemediationPlanner()
        self.executor = SafeExecutor(self.runtime_root, self.registry)
        self.validator = ExecutionValidator()

    def _due_at(self, severity: str) -> str:
        hours = self.policy.sla_hours.get(severity, self.policy.sla_hours["info"])
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    def ingest_run(
        self,
        run_root: str | Path,
        *,
        actor: str = "audit-ingest",
    ) -> dict[str, Any]:
        root = Path(run_root)
        portfolio = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
        created = 0
        existing = 0
        skipped = 0
        for report_path in sorted((root / "repositories").glob("*.json")):
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") != "audited" or not report.get("reviewer_passed"):
                skipped += 1
                continue
            repository = report["repository"]
            for raw in report.get("findings", []):
                work_item_id = stable_id(
                    "WORK",
                    repository["full_name"],
                    raw["finding_id"],
                    repository.get("commit_sha") or "no-commit",
                )
                item = WorkItem(
                    work_item_id=work_item_id,
                    finding_id=raw["finding_id"],
                    repository=repository["full_name"],
                    source_commit=repository.get("commit_sha"),
                    source_run_id=portfolio["run_id"],
                    title=raw["title"],
                    category=raw["category"],
                    severity=raw["severity"],
                    recommendation=raw["recommendation"],
                    evidence=list(raw["evidence"]),
                    created_by=actor,
                    due_at=self._due_at(raw["severity"]),
                    max_retries=self.policy.workflow_max_retries,
                )
                _, was_created = self.store.create_work_item(item)
                if was_created:
                    created += 1
                else:
                    existing += 1
        return {
            "run_id": portfolio["run_id"],
            "created": created,
            "existing": existing,
            "skipped_repository_reports": skipped,
        }

    def triage(
        self,
        work_item_id: str,
        *,
        actor: str,
        owner: str,
    ) -> WorkItem:
        if not actor.strip() or not owner.strip():
            raise ValueError("actor and owner are required")
        return self.store.transition(
            work_item_id,
            "triaged",
            actor=actor,
            reason="Ownership and SLA confirmed.",
            updates={"owner": owner},
        )

    def plan(self, work_item_id: str, *, actor: str) -> WorkItem:
        item = self.store.get_work_item(work_item_id)
        if item.state != "triaged":
            raise ValueError("work item must be triaged before planning")
        plan = self.planner.create_plan(
            item,
            max_retries=self.policy.workflow_max_retries,
        )
        if actor != self.planner.name:
            plan = type(plan)(
                **{**plan.to_dict(), "planner": actor, "steps": plan.steps,
                   "validation_checks": plan.validation_checks}
            )
        tool = self.registry.get(plan.tool_name)
        if not tool.enabled or tool.side_effects:
            raise PermissionError("planner selected a tool outside the safe policy")
        self.store.save_plan(plan)
        self.store.transition(
            work_item_id,
            "planned",
            actor=actor,
            reason="Bounded remediation plan created.",
            updates={"plan_id": plan.plan_id},
        )
        return self.store.transition(
            work_item_id,
            "awaiting_execution_approval",
            actor=actor,
            reason="All remediation execution requires human approval.",
        )

    def approve_execution(
        self,
        work_item_id: str,
        *,
        actor: str,
        decision: str,
        comment: str,
    ) -> WorkItem:
        item = self.store.get_work_item(work_item_id)
        if item.state != "awaiting_execution_approval":
            raise ValueError("work item is not awaiting execution approval")
        plan = self.store.get_plan(work_item_id)
        if actor in {item.created_by, plan.planner}:
            raise PermissionError("execution approver must be independent")
        approval = Approval(
            work_item_id=work_item_id,
            stage="execution",
            actor=actor,
            decision=decision,
            comment=comment,
        )
        self.store.add_approval(approval)
        target = "approved" if decision == "approved" else "manual_review"
        return self.store.transition(
            work_item_id,
            target,
            actor=actor,
            reason=f"Execution approval decision: {decision}.",
        )

    def execute(self, work_item_id: str, *, actor: str) -> WorkItem:
        item = self.store.get_work_item(work_item_id)
        if item.state != "approved":
            raise ValueError("work item must be approved before execution")
        approvals = [
            item
            for item in self.store.approvals(work_item_id)
            if item.stage == "execution" and item.decision == "approved"
        ]
        if not approvals:
            raise PermissionError("approved execution record is required")
        if actor == approvals[-1].actor:
            raise PermissionError("executor must be independent from approver")
        plan = self.store.get_plan(work_item_id)
        executing = self.store.transition(
            work_item_id,
            "executing",
            actor=actor,
            reason="Approved dry-run execution started.",
        )
        attempt = executing.retry_count + 1
        try:
            result = self.executor.execute(
                item=executing,
                plan=plan,
                actor=actor,
                attempt=attempt,
            )
            self.store.add_execution(result)
            return self.store.transition(
                work_item_id,
                "validating",
                actor=actor,
                reason="Dry-run artifact generated; validation required.",
                updates={"retry_count": attempt, "last_error": None},
            )
        except Exception as error:
            if attempt < executing.max_retries:
                target = "approved"
            else:
                target = "manual_review"
            return self.store.transition(
                work_item_id,
                target,
                actor=actor,
                reason="Execution failed; bounded retry/fallback applied.",
                updates={
                    "retry_count": attempt,
                    "last_error": f"{type(error).__name__}: {error}",
                },
            )

    def validate(self, work_item_id: str, *, actor: str) -> WorkItem:
        item = self.store.get_work_item(work_item_id)
        if item.state != "validating":
            raise ValueError("work item is not awaiting validation")
        result = self.store.latest_execution(work_item_id)
        if actor == result.actor:
            raise PermissionError("validator must be independent from executor")
        passed, failures = self.validator.validate(result)
        self.store.append_event(
            work_item_id=work_item_id,
            event_type="validation_completed",
            actor=actor,
            payload={"passed": passed, "failures": failures},
        )
        if passed:
            return self.store.transition(
                work_item_id,
                "awaiting_closure_approval",
                actor=actor,
                reason="Independent validation passed.",
            )
        target = (
            "approved"
            if item.retry_count < item.max_retries
            else "manual_review"
        )
        return self.store.transition(
            work_item_id,
            target,
            actor=actor,
            reason="Validation failed; retry or manual fallback selected.",
            updates={"last_error": ",".join(failures)},
        )

    def approve_closure(
        self,
        work_item_id: str,
        *,
        actor: str,
        decision: str,
        comment: str,
    ) -> WorkItem:
        item = self.store.get_work_item(work_item_id)
        if item.state != "awaiting_closure_approval":
            raise ValueError("work item is not awaiting closure approval")
        prior_actors = {item.created_by}
        prior_actors.update(approval.actor for approval in self.store.approvals(work_item_id))
        prior_actors.add(self.store.latest_execution(work_item_id).actor)
        if actor in prior_actors:
            raise PermissionError("closure approver must be independent")
        approval = Approval(
            work_item_id=work_item_id,
            stage="closure",
            actor=actor,
            decision=decision,
            comment=comment,
        )
        self.store.add_approval(approval)
        target = "closed" if decision == "approved" else "manual_review"
        return self.store.transition(
            work_item_id,
            target,
            actor=actor,
            reason=f"Closure approval decision: {decision}.",
        )

    def status(self) -> dict[str, Any]:
        items = self.store.list_work_items()
        now = datetime.now(timezone.utc)
        overdue = [
            item.work_item_id
            for item in items
            if item.due_at
            and item.state not in {"closed", "failed"}
            and datetime.fromisoformat(item.due_at) < now
        ]
        return {
            "work_item_count": len(items),
            "state_counts": dict(sorted(Counter(item.state for item in items).items())),
            "severity_counts": dict(
                sorted(Counter(item.severity for item in items).items())
            ),
            "overdue_count": len(overdue),
            "overdue_work_items": overdue,
            "event_count": len(self.store.events()),
            "event_chain_valid": self.store.verify_event_chain(),
            "source_repository_mutation": False,
            "production_execution": False,
        }
