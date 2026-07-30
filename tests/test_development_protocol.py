from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ae_control_plane.development import (
    DevelopmentController,
    DevelopmentPolicy,
    RepositoryOnboardingController,
    RepositoryRegistry,
)


def git(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


class DevelopmentProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(["init", "-b", "main"], self.source)
        (self.source / "README.md").write_text("# Fixture\n", encoding="utf-8")
        git(["add", "README.md"], self.source)
        git(
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "initial",
            ],
            self.source,
        )
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "mode": "governed_development",
                    "allowed_owners": ["SuriyaBoon"],
                    "workspace_root": "development/tasks",
                    "limits": {
                        "max_changed_files": 5,
                        "max_added_lines": 50,
                        "max_deleted_lines": 20,
                        "max_file_bytes": 4096,
                        "max_repair_attempts": 2,
                        "test_timeout_seconds": 30,
                    },
                    "allowed_test_executables": [
                        "python",
                        Path(sys.executable).name.lower()
                    ],
                    "test_execution": {
                        "mode": "host",
                        "trusted_host_execution": True,
                    },
                    "guardrails": {
                        "require_independent_review": True,
                        "require_human_publish_approval": True,
                        "draft_pr_only": True,
                        "direct_default_branch_push": False,
                        "production_actions_enabled": False,
                        "required_post_merge_checks": ["validate"],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.registry_path = self.root / "repositories.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "repositories": [
                        {
                            "name": "fixture",
                            "full_name": "SuriyaBoon/fixture",
                            "clone_url": str(self.source),
                            "default_branch": "main",
                            "test_commands": [
                                [sys.executable, "-c", "print('tests passed')"]
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.controller = DevelopmentController(
            policy=DevelopmentPolicy.load(self.policy_path),
            registry=RepositoryRegistry(self.registry_path),
            runtime_root=self.root / "runtime",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start_and_prepare(self) -> str:
        task = self.controller.start(
            repository_name="fixture",
            intent="Add safe documentation",
            acceptance_criteria=["README contains the governed note"],
            actor="requester",
        )
        self.controller.plan(task["task_id"], actor="planner-agent")
        prepared = self.controller.prepare(
            task["task_id"], actor="workspace-manager"
        )
        self.assertNotEqual(prepared["source_sha"], "")
        self.assertEqual(
            git(["branch", "--show-current"], Path(prepared["workspace"])),
            prepared["branch"],
        )
        return task["task_id"]

    def test_prepare_uses_registered_clone_url_not_stale_local_path(self) -> None:
        stale = self.root / "stale-local"
        stale.mkdir()
        git(["init", "-b", "main"], stale)
        (stale / "README.md").write_text("# Stale local checkout\n", encoding="utf-8")
        git(["add", "README.md"], stale)
        git(
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "stale",
            ],
            stale,
        )
        self.controller.registry.repositories["fixture"]["local_path"] = str(stale)
        expected_remote_sha = git(["rev-parse", "HEAD"], self.source)
        stale_sha = git(["rev-parse", "HEAD"], stale)

        task_id = self.start_and_prepare()
        prepared = self.controller.store.load(task_id)

        self.assertEqual(prepared["source_sha"], expected_remote_sha)
        self.assertNotEqual(prepared["source_sha"], stale_sha)
        self.assertEqual(
            (Path(prepared["workspace"]) / "README.md").read_text(encoding="utf-8"),
            "# Fixture\n",
        )

    def apply_fixture_change(self, task_id: str, content: str = "# Fixture\nGoverned.\n") -> None:
        path = self.root / "change-set.json"
        path.write_text(
            json.dumps(
                {
                    "summary": "Update documentation",
                    "operations": [
                        {"op": "write", "path": "README.md", "content": content}
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.controller.apply_changes(
            task_id,
            change_set_path=path,
            actor="coder-agent",
        )

    def test_end_to_end_stops_at_human_publish_gate(self) -> None:
        task_id = self.start_and_prepare()
        self.apply_fixture_change(task_id)
        tested = self.controller.test(task_id, actor="test-runner")
        self.assertEqual(tested["state"], "tests_passed")
        reviewed = self.controller.review(
            task_id,
            actor="reviewer-agent",
            acceptance_evidence=["README content inspected after tests."],
        )
        self.assertEqual(reviewed["state"], "awaiting_publish_approval")
        approved = self.controller.approve_publish(
            task_id,
            actor="repo-owner",
            confirmation=f"APPROVE {task_id}",
            comment="Reviewed for a draft PR.",
        )
        self.assertEqual(approved["state"], "publish_approved")
        evidence = self.controller.build_evidence(
            task_id, actor="evidence-agent"
        )
        manifest_path = Path(evidence["evidence_manifest"]["path"])
        self.assertTrue(manifest_path.is_file())
        manifest = self.controller.verify_evidence_manifest(manifest_path)
        self.assertEqual(manifest["schema_version"], "2.0.0")
        self.assertEqual(manifest["task_id"], task_id)
        self.assertTrue(self.controller.store.verify_chain(task_id))
        # Live state/events changed when the manifest reference was recorded,
        # but the packaged snapshot must remain independently verifiable.
        self.controller.verify_evidence_manifest(manifest_path)
        snapshot = manifest_path.parent / "state.json"
        snapshot.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.controller.verify_evidence_manifest(manifest_path)
        self.assertEqual(
            (self.source / "README.md").read_text(encoding="utf-8"),
            "# Fixture\n",
        )

    def _published_fixture(self) -> tuple[str, str]:
        task_id = self.start_and_prepare()
        self.apply_fixture_change(task_id)
        self.controller.test(task_id, actor="test-runner")
        self.controller.review(
            task_id,
            actor="reviewer-agent",
            acceptance_evidence=["README content inspected after tests."],
        )
        approved = self.controller.approve_publish(
            task_id,
            actor="repo-owner",
            confirmation=f"APPROVE {task_id}",
            comment="Reviewed for a draft PR.",
        )
        reviewed_head = "a" * 40
        self.controller.store.transition(
            approved,
            "published",
            actor="publisher",
            reason="Synthetic published fixture.",
            updates={
                "pull_request": {
                    "number": 7,
                    "url": "https://example.invalid/pull/7",
                    "draft": True,
                    "head_sha": reviewed_head,
                }
            },
        )
        return task_id, reviewed_head

    def test_merge_verification_requires_immutable_head_and_required_ci(self) -> None:
        task_id, reviewed_head = self._published_fixture()
        auth = {"to" + "ken": "synthetic"}
        changed_pr = {
            "merged_at": "2026-01-01T00:00:00Z",
            "merge_commit_sha": "b" * 40,
            "head": {"sha": "c" * 40},
        }
        with patch.object(
            self.controller,
            "_github_request",
            return_value=changed_pr,
        ):
            with self.assertRaisesRegex(PermissionError, "reviewed published head"):
                self.controller.verify_merge(
                    task_id, actor="post-merge-verifier", **auth
                )

        matching_pr = {
            **changed_pr,
            "head": {"sha": reviewed_head},
        }
        with patch.object(
            self.controller,
            "_github_request",
            side_effect=[matching_pr, {"check_runs": []}],
        ):
            with self.assertRaisesRegex(RuntimeError, "checks are missing"):
                self.controller.verify_merge(
                    task_id, actor="post-merge-verifier", **auth
                )

        successful_checks = {
            "check_runs": [
                {
                    "name": "validate",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
        with patch.object(
            self.controller,
            "_github_request",
            side_effect=[matching_pr, successful_checks],
        ):
            verified = self.controller.verify_merge(
                task_id, actor="post-merge-verifier", **auth
            )
        self.assertEqual(verified["state"], "merged_verified")
        self.assertEqual(
            verified["merge_verification"]["required_checks"],
            ["validate"],
        )

    def test_stale_task_transition_is_rejected(self) -> None:
        task = self.controller.start(
            repository_name="fixture",
            intent="Test stale transitions",
            acceptance_criteria=["Only one transition is recorded"],
            actor="requester",
        )
        first = self.controller.store.load(task["task_id"])
        stale = self.controller.store.load(task["task_id"])
        self.controller.store.transition(
            first,
            "planned",
            actor="planner-one",
            reason="First transition.",
        )
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.controller.store.transition(
                stale,
                "planned",
                actor="planner-two",
                reason="Stale transition.",
            )

    def test_path_traversal_is_blocked(self) -> None:
        task_id = self.start_and_prepare()
        path = self.root / "bad-change-set.json"
        path.write_text(
            json.dumps(
                {
                    "summary": "Escape",
                    "operations": [
                        {"op": "write", "path": "../escape.txt", "content": "bad"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(PermissionError):
            self.controller.apply_changes(
                task_id, change_set_path=path, actor="coder-agent"
            )

    def test_coder_cannot_test_or_review_own_change(self) -> None:
        task_id = self.start_and_prepare()
        self.apply_fixture_change(task_id)
        with self.assertRaises(PermissionError):
            self.controller.test(task_id, actor="coder-agent")
        self.controller.test(task_id, actor="test-runner")
        with self.assertRaises(PermissionError):
            self.controller.review(
                task_id,
                actor="coder-agent",
                acceptance_evidence=["README content inspected."],
            )

    def test_secret_detection_fails_review(self) -> None:
        task_id = self.start_and_prepare()
        secret_name = "api" + "_key"
        secret_value = "synthetic-" + ("x" * 16)
        self.apply_fixture_change(
            task_id,
            f'# Fixture\n{secret_name} = "{secret_value}"\n',
        )
        self.controller.test(task_id, actor="test-runner")
        task = self.controller.review(
            task_id,
            actor="reviewer-agent",
            acceptance_evidence=["README content inspected."],
        )
        self.assertEqual(task["state"], "review_failed")
        self.assertIn("potential_secret_detected", task["review"]["failures"])

    def test_publish_approval_requires_exact_confirmation(self) -> None:
        task_id = self.start_and_prepare()
        self.apply_fixture_change(task_id)
        self.controller.test(task_id, actor="test-runner")
        self.controller.review(
            task_id,
            actor="reviewer-agent",
            acceptance_evidence=["README content inspected."],
        )
        with self.assertRaises(PermissionError):
            self.controller.approve_publish(
                task_id,
                actor="repo-owner",
                confirmation="yes",
                comment="Too vague.",
            )

    def test_task_can_be_cancelled_before_publish_approval(self) -> None:
        task = self.controller.start(
            repository_name="fixture",
            intent="Superseded implementation plan",
            acceptance_criteria=["Replacement task is used"],
            actor="requester",
        )
        cancelled = self.controller.cancel(
            task["task_id"],
            actor="requester",
            reason="Task exceeds the approved change budget.",
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(self.controller.store.verify_chain(task["task_id"]))
        with self.assertRaises(ValueError):
            self.controller.plan(task["task_id"], actor="planner")

    def test_policy_rejects_direct_main_push(self) -> None:
        path = self.root / "unsafe-policy.json"
        payload = {
            "mode": "governed_development",
            "allowed_owners": ["SuriyaBoon"],
            "workspace_root": "x",
            "limits": {
                "max_changed_files": 1,
                "max_added_lines": 1,
                "max_deleted_lines": 1,
                "max_file_bytes": 1,
                "max_repair_attempts": 1,
                "test_timeout_seconds": 1,
            },
            "allowed_test_executables": ["python"],
            "guardrails": {
                "require_independent_review": True,
                "require_human_publish_approval": True,
                "draft_pr_only": True,
                "direct_default_branch_push": True,
                "production_actions_enabled": False,
                "required_post_merge_checks": ["validate"],
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            DevelopmentPolicy.load(path)


class RepositoryOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        DevelopmentProtocolTests.setUp(self)
        self.new_source = self.root / "new-source"
        self.new_source.mkdir()
        git(["init", "-b", "main"], self.new_source)
        (self.new_source / "new_project").mkdir()
        (self.new_source / "new_project" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (self.new_source / "tests").mkdir()
        (self.new_source / "tests" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (self.new_source / "tests" / "test_smoke.py").write_text(
            "import unittest\n\n"
            "class SmokeTest(unittest.TestCase):\n"
            "    def test_true(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.new_source / "pyproject.toml").write_text(
            '[project]\nname="new-project"\nversion="0.1.0"\n',
            encoding="utf-8",
        )
        git(["add", "--all"], self.new_source)
        git(
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "initial",
            ],
            self.new_source,
        )
        self.overlay = self.root / "runtime" / "development" / "active-repositories.json"
        self.registry = RepositoryRegistry(self.registry_path, self.overlay)
        self.onboarding = RepositoryOnboardingController(
            policy=DevelopmentPolicy.load(self.policy_path),
            registry=self.registry,
            runtime_root=self.root / "runtime",
        )
        self.metadata = {
            "id": 2,
            "name": "new-project",
            "full_name": "SuriyaBoon/new-project",
            "visibility": "public",
            "default_branch": "main",
            "clone_url": str(self.new_source),
            "archived": False,
        }

    def tearDown(self) -> None:
        DevelopmentProtocolTests.tearDown(self)

    def test_new_repository_is_quarantined_until_activated(self) -> None:
        result = self.onboarding.discover(
            [self.metadata], actor="discovery-agent"
        )
        self.assertEqual(result["discovered"], 1)
        with self.assertRaises(KeyError):
            self.registry.get("new-project")

        assessed = self.onboarding.assess(
            "SuriyaBoon/new-project", actor="framework-agent"
        )
        self.assertEqual(assessed["state"], "awaiting_onboarding_approval")
        self.assertIn("python", assessed["frameworks"])
        self.assertEqual(
            assessed["proposed_test_commands"][0],
            [
                "python",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
        )

        approved = self.onboarding.approve(
            "SuriyaBoon/new-project",
            actor="repo-owner",
            confirmation="APPROVE ONBOARD SuriyaBoon/new-project",
            comment="Approve isolated smoke validation and activation.",
        )
        self.assertEqual(approved["state"], "approved")
        active = self.onboarding.activate(
            "SuriyaBoon/new-project", actor="smoke-runner"
        )
        self.assertEqual(active["state"], "active")
        self.assertTrue(active["smoke_result"]["passed"])
        self.assertEqual(
            self.registry.get("new-project")["full_name"],
            "SuriyaBoon/new-project",
        )
        self.assertTrue(
            self.onboarding.store.verify_chain("SuriyaBoon/new-project")
        )

    def test_contract_can_be_replaced_before_onboarding_approval(self) -> None:
        self.onboarding.discover([self.metadata], actor="discovery-agent")
        self.onboarding.assess(
            "SuriyaBoon/new-project", actor="framework-agent"
        )

        replacement = [
            ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
        ]
        updated = self.onboarding.set_contract(
            "SuriyaBoon/new-project",
            commands=replacement,
            actor="contract-owner",
        )

        self.assertEqual(updated["state"], "awaiting_onboarding_approval")
        self.assertEqual(updated["proposed_test_commands"], replacement)
        self.assertIsNone(updated["approval"])
        self.assertTrue(
            self.onboarding.store.verify_chain("SuriyaBoon/new-project")
        )

    def test_onboarding_approval_requires_independent_exact_confirmation(self) -> None:
        self.onboarding.discover([self.metadata], actor="discovery-agent")
        self.onboarding.assess(
            "SuriyaBoon/new-project", actor="framework-agent"
        )
        with self.assertRaises(PermissionError):
            self.onboarding.approve(
                "SuriyaBoon/new-project",
                actor="framework-agent",
                confirmation="APPROVE ONBOARD SuriyaBoon/new-project",
                comment="Self approval.",
            )
        with self.assertRaises(PermissionError):
            self.onboarding.approve(
                "SuriyaBoon/new-project",
                actor="repo-owner",
                confirmation="yes",
                comment="Vague approval.",
            )

    def test_suspension_removes_repository_from_active_registry(self) -> None:
        self.onboarding.discover([self.metadata], actor="discovery-agent")
        self.onboarding.assess(
            "SuriyaBoon/new-project", actor="framework-agent"
        )
        self.onboarding.approve(
            "SuriyaBoon/new-project",
            actor="repo-owner",
            confirmation="APPROVE ONBOARD SuriyaBoon/new-project",
            comment="Approved.",
        )
        self.onboarding.activate(
            "SuriyaBoon/new-project", actor="smoke-runner"
        )
        suspended = self.onboarding.suspend(
            "new-project",
            actor="repo-owner",
            confirmation="SUSPEND SuriyaBoon/new-project",
            reason="Maintenance window.",
        )
        self.assertEqual(suspended["state"], "suspended")
        with self.assertRaises(PermissionError):
            self.registry.get("new-project")
        resumed = self.onboarding.resume_onboarding(
            "SuriyaBoon/new-project",
            actor="repo-owner",
            confirmation="RESUME ONBOARDING SuriyaBoon/new-project",
            reason="Maintenance complete.",
        )
        self.assertEqual(resumed["state"], "discovered")
        with self.assertRaises(PermissionError):
            self.registry.get("new-project")


if __name__ == "__main__":
    unittest.main()
