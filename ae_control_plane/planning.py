from __future__ import annotations

from dataclasses import dataclass

from .lifecycle import RemediationPlan, WorkItem, stable_id


@dataclass(frozen=True)
class ToolSpec:
    name: str
    enabled: bool
    side_effects: bool
    requires_human_approval: bool
    purpose: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "side_effects": self.side_effects,
            "requires_human_approval": self.requires_human_approval,
            "purpose": self.purpose,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools = {
            "mock_remediation": ToolSpec(
                name="mock_remediation",
                enabled=True,
                side_effects=False,
                requires_human_approval=True,
                purpose="Produce a bounded remediation proposal and evidence artifact.",
            ),
            "github_draft_pr": ToolSpec(
                name="github_draft_pr",
                enabled=False,
                side_effects=True,
                requires_human_approval=True,
                purpose="Reserved boundary for an approved isolated-branch change.",
            ),
            "live_infrastructure_action": ToolSpec(
                name="live_infrastructure_action",
                enabled=False,
                side_effects=True,
                requires_human_approval=True,
                purpose="Reserved boundary; production actions are prohibited.",
            ),
        }

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def list(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]


class RemediationPlanner:
    name = "planner"

    CATEGORY_STEPS = {
        "architecture": "Draft an architecture and execution-flow documentation change.",
        "workflow": "Draft explicit states, retries, fallback, and terminal behavior.",
        "repository_integrity": "Prepare an integrity repair and independent verification checklist.",
        "secret_handling": "Prepare secret removal, rotation, and history-review instructions.",
        "code_execution": "Prepare a safe argument-array and allowlist remediation.",
        "live_action_boundary": "Prepare fail-closed dry-run and approval controls.",
        "testing": "Draft focused tests and negative controls.",
        "continuous_integration": "Draft least-privilege CI validation.",
        "documentation": "Draft reproducible validation and scope documentation.",
        "claim_integrity": "Align claims with verified implementation evidence.",
        "integration": "Draft a versioned schema, fixture, and contract test.",
        "governance": "Draft ownership, approval, evidence, verification, and closure controls.",
    }

    def create_plan(self, item: WorkItem, *, max_retries: int) -> RemediationPlan:
        category_step = self.CATEGORY_STEPS.get(
            item.category,
            "Prepare a bounded remediation proposal for manual review.",
        )
        plan_id = stable_id(
            "PLAN",
            item.work_item_id,
            item.source_commit or "no-commit",
        )
        return RemediationPlan(
            plan_id=plan_id,
            work_item_id=item.work_item_id,
            planner=self.name,
            tool_name="mock_remediation",
            execution_mode="dry_run",
            steps=(
                "Reconfirm finding evidence against the captured source identity.",
                category_step,
                "Generate a dry-run artifact without changing the source repository.",
                "Validate artifact integrity and the source-mutation guard.",
                "Request independent closure approval.",
            ),
            validation_checks=(
                "artifact_exists",
                "artifact_sha256_matches",
                "source_repository_mutation_is_false",
                "execution_actor_is_not_execution_approver",
                "closure_approver_is_independent",
            ),
            fallback="Move to manual_review with the error and event-chain evidence.",
            max_retries=max_retries,
            source_repository_mutation=False,
        )
