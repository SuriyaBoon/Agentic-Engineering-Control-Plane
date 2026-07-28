from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ae_control_plane.lifecycle import WorkItem, stable_id
from ae_control_plane.planning import ToolRegistry
from ae_control_plane.policy import AuditPolicy
from ae_control_plane.store import WorkflowStore
from ae_control_plane.workflow import GovernedWorkflow


ROOT = Path(__file__).parents[1]
POLICY = ROOT / "config" / "policy.json"


def work_item() -> WorkItem:
    return WorkItem(
        work_item_id="WORK-fixture",
        finding_id="AE-TEST-1",
        repository="SuriyaBoon/fixture",
        source_commit="a" * 40,
        source_run_id="RUN-1",
        title="Fixture finding",
        category="testing",
        severity="medium",
        recommendation="Add negative tests.",
        evidence=["tests/test_fixture.py"],
        due_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def write_audit_run(root: Path) -> Path:
    run = root / "run"
    (run / "repositories").mkdir(parents=True)
    portfolio = {
        "schema_version": "1.0.0",
        "owner": "SuriyaBoon",
        "run_id": "RUN-1",
        "repository_count": 1,
    }
    report = {
        "repository": {
            "id": 1,
            "name": "fixture",
            "full_name": "SuriyaBoon/fixture",
            "visibility": "public",
            "default_branch": "main",
            "clone_url": "",
            "archived": False,
            "commit_sha": "a" * 40,
        },
        "status": "audited",
        "reviewer_passed": True,
        "findings": [
            {
                "finding_id": "AE-TEST-1",
                "agent": "testing",
                "category": "testing",
                "severity": "medium",
                "title": "Fixture finding",
                "evidence": ["tests/test_fixture.py"],
                "recommendation": "Add negative tests.",
                "confidence": 1.0,
            }
        ],
    }
    (run / "portfolio.json").write_text(json.dumps(portfolio), encoding="utf-8")
    (run / "repositories" / "fixture.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )
    return run


class StoreTests(unittest.TestCase):
    def test_ingest_identity_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "workflow.db")
            first, created = store.create_work_item(work_item())
            second, duplicate_created = store.create_work_item(work_item())
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(first.work_item_id, second.work_item_id)
            self.assertEqual(len(store.list_work_items()), 1)

    def test_invalid_state_transition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "workflow.db")
            store.create_work_item(work_item())
            with self.assertRaises(ValueError):
                store.transition(
                    "WORK-fixture",
                    "closed",
                    actor="tester",
                    reason="Invalid direct closure.",
                )

    def test_event_hash_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "workflow.db"
            store = WorkflowStore(path)
            store.create_work_item(work_item())
            self.assertTrue(store.verify_event_chain())
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE sequence = 1",
                    ('{"tampered":true}',),
                )
                connection.commit()
            self.assertFalse(store.verify_event_chain())


class GuardrailTests(unittest.TestCase):
    def test_side_effecting_tools_are_disabled(self) -> None:
        registry = ToolRegistry()
        self.assertTrue(registry.get("mock_remediation").enabled)
        self.assertFalse(registry.get("github_draft_pr").enabled)
        self.assertFalse(registry.get("live_infrastructure_action").enabled)


class GovernedWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workflow = GovernedWorkflow(
            policy=AuditPolicy(POLICY),
            runtime_root=self.root / "runtime",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest_and_plan(self) -> str:
        result = self.workflow.ingest_run(write_audit_run(self.root))
        self.assertEqual(result["created"], 1)
        item = self.workflow.store.list_work_items()[0]
        self.workflow.triage(
            item.work_item_id,
            actor="triage-lead",
            owner="repo-owner",
        )
        self.workflow.plan(item.work_item_id, actor="planner")
        return item.work_item_id

    def test_complete_closed_loop_with_independent_roles(self) -> None:
        work_item_id = self._ingest_and_plan()
        self.workflow.approve_execution(
            work_item_id,
            actor="change-approver",
            decision="approved",
            comment="Dry-run approved.",
        )
        self.workflow.execute(work_item_id, actor="safe-executor")
        self.workflow.validate(work_item_id, actor="validator")
        closed = self.workflow.approve_closure(
            work_item_id,
            actor="risk-owner",
            decision="approved",
            comment="Simulation evidence verified.",
        )
        self.assertEqual(closed.state, "closed")
        status = self.workflow.status()
        self.assertEqual(status["state_counts"], {"closed": 1})
        self.assertTrue(status["event_chain_valid"])
        self.assertFalse(status["source_repository_mutation"])
        self.assertFalse(status["production_execution"])

    def test_planner_cannot_approve_own_plan(self) -> None:
        work_item_id = self._ingest_and_plan()
        with self.assertRaises(PermissionError):
            self.workflow.approve_execution(
                work_item_id,
                actor="planner",
                decision="approved",
                comment="Self approval must fail.",
            )

    def test_executor_cannot_be_execution_approver(self) -> None:
        work_item_id = self._ingest_and_plan()
        self.workflow.approve_execution(
            work_item_id,
            actor="change-approver",
            decision="approved",
            comment="Approved.",
        )
        with self.assertRaises(PermissionError):
            self.workflow.execute(work_item_id, actor="change-approver")

    def test_validator_cannot_validate_own_execution(self) -> None:
        work_item_id = self._ingest_and_plan()
        self.workflow.approve_execution(
            work_item_id,
            actor="change-approver",
            decision="approved",
            comment="Approved.",
        )
        self.workflow.execute(work_item_id, actor="safe-executor")
        with self.assertRaises(PermissionError):
            self.workflow.validate(work_item_id, actor="safe-executor")

    def test_closure_approver_must_be_independent(self) -> None:
        work_item_id = self._ingest_and_plan()
        self.workflow.approve_execution(
            work_item_id,
            actor="change-approver",
            decision="approved",
            comment="Approved.",
        )
        self.workflow.execute(work_item_id, actor="safe-executor")
        self.workflow.validate(work_item_id, actor="validator")
        with self.assertRaises(PermissionError):
            self.workflow.approve_closure(
                work_item_id,
                actor="change-approver",
                decision="approved",
                comment="Self closure must fail.",
            )

    def test_tampered_execution_artifact_returns_to_retry(self) -> None:
        work_item_id = self._ingest_and_plan()
        self.workflow.approve_execution(
            work_item_id,
            actor="change-approver",
            decision="approved",
            comment="Approved.",
        )
        self.workflow.execute(work_item_id, actor="safe-executor")
        execution = self.workflow.store.latest_execution(work_item_id)
        Path(execution.artifact_path).write_text("tampered", encoding="utf-8")
        item = self.workflow.validate(work_item_id, actor="validator")
        self.assertEqual(item.state, "approved")
        self.assertIn("artifact_sha256_mismatch", item.last_error)

    def test_ingest_is_idempotent_across_retries(self) -> None:
        run = write_audit_run(self.root)
        first = self.workflow.ingest_run(run)
        second = self.workflow.ingest_run(run)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], 1)

    def test_stable_id_is_deterministic(self) -> None:
        self.assertEqual(
            stable_id("WORK", "a", "b"),
            stable_id("WORK", "a", "b"),
        )


if __name__ == "__main__":
    unittest.main()
