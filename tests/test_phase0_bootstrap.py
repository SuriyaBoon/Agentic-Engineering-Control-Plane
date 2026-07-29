from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ae_control_plane.development import (
    DevelopmentController,
    DevelopmentPolicy,
    RepositoryOnboardingController,
    RepositoryRegistry,
)


def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


class PhaseZeroBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(["init", "-b", "main"], self.source)
        (self.source / "app").mkdir()
        (self.source / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.source / "tests").mkdir()
        (self.source / "tests" / "test_smoke.py").write_text(
            "import unittest\n\n"
            "class Smoke(unittest.TestCase):\n"
            "    def test_true(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.source / "pyproject.toml").write_text(
            '[project]\nname="fixture"\nversion="0.1.0"\n',
            encoding="utf-8",
        )
        git(["add", "--all"], self.source)
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
                        Path(sys.executable).name.lower(),
                    ],
                    "guardrails": {
                        "require_independent_review": True,
                        "require_human_publish_approval": True,
                        "draft_pr_only": True,
                        "direct_default_branch_push": False,
                        "production_actions_enabled": False,
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
                                [sys.executable, "-c", "print('passed')"]
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.policy = DevelopmentPolicy.load(self.policy_path)
        self.registry = RepositoryRegistry(
            self.registry_path, self.root / "active-repositories.json"
        )
        self.runtime = self.root / "runtime"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _controller(self) -> DevelopmentController:
        return DevelopmentController(
            policy=self.policy,
            registry=self.registry,
            runtime_root=self.runtime,
        )

    def _prepared_task(self) -> tuple[DevelopmentController, str]:
        controller = self._controller()
        task = controller.start(
            repository_name="fixture",
            intent="Add governed fixture",
            acceptance_criteria=["Fixture is updated"],
            actor="requester",
        )
        controller.plan(task["task_id"], actor="planner")
        controller.prepare(task["task_id"], actor="workspace-manager")
        return controller, task["task_id"]

    def test_change_set_escape_and_self_review_are_rejected(self) -> None:
        controller, task_id = self._prepared_task()
        bad = self.root / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "summary": "escape",
                    "operations": [
                        {"op": "write", "path": "../escape", "content": "bad"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(PermissionError):
            controller.apply_changes(
                task_id, change_set_path=bad, actor="coder-agent"
            )
        good = self.root / "good.json"
        good.write_text(
            json.dumps(
                {
                    "summary": "safe",
                    "operations": [
                        {"op": "write", "path": "NOTE.md", "content": "safe\n"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        controller.apply_changes(
            task_id, change_set_path=good, actor="coder-agent"
        )
        with self.assertRaises(PermissionError):
            controller.test(task_id, actor="coder-agent")
        controller.test(task_id, actor="test-runner")
        with self.assertRaises(PermissionError):
            controller.review(
                task_id,
                actor="coder-agent",
                acceptance_evidence=["Fixture inspected"],
            )

    def test_exact_publish_approval_and_event_chain(self) -> None:
        controller, task_id = self._prepared_task()
        change = self.root / "change.json"
        change.write_text(
            json.dumps(
                {
                    "summary": "safe",
                    "operations": [
                        {"op": "write", "path": "NOTE.md", "content": "safe\n"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        controller.apply_changes(
            task_id, change_set_path=change, actor="coder-agent"
        )
        controller.test(task_id, actor="test-runner")
        controller.review(
            task_id,
            actor="independent-reviewer",
            acceptance_evidence=["Fixture inspected"],
        )
        with self.assertRaises(PermissionError):
            controller.approve_publish(
                task_id,
                actor="repo-owner",
                confirmation="yes",
                comment="vague",
            )
        approved = controller.approve_publish(
            task_id,
            actor="repo-owner",
            confirmation=f"APPROVE {task_id}",
            comment="Draft PR only.",
        )
        self.assertEqual(approved["state"], "publish_approved")
        self.assertTrue(controller.store.verify_chain(task_id))

    def test_onboarding_detects_and_replaces_contract_before_approval(self) -> None:
        onboarding = RepositoryOnboardingController(
            policy=self.policy,
            registry=self.registry,
            runtime_root=self.runtime,
        )
        full_name = "SuriyaBoon/new-fixture"
        metadata = {
            "id": 2,
            "name": "new-fixture",
            "full_name": full_name,
            "visibility": "public",
            "default_branch": "main",
            "clone_url": str(self.source),
            "archived": False,
        }
        onboarding.discover([metadata], actor="discovery-agent")
        assessed = onboarding.assess(full_name, actor="framework-agent")
        expected = [
            "python",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ]
        self.assertEqual(assessed["proposed_test_commands"][0], expected)
        replaced = onboarding.set_contract(
            full_name,
            commands=[expected],
            actor="contract-owner",
        )
        self.assertEqual(replaced["state"], "awaiting_onboarding_approval")
        with self.assertRaises(PermissionError):
            onboarding.approve(
                full_name,
                actor="repo-owner",
                confirmation="yes",
                comment="vague",
            )
        self.assertTrue(onboarding.store.verify_chain(full_name))

    def test_policy_rejects_unsafe_publication_modes(self) -> None:
        payload = json.loads(self.policy_path.read_text(encoding="utf-8"))
        payload["guardrails"]["direct_default_branch_push"] = True
        self.policy_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            DevelopmentPolicy.load(self.policy_path)


if __name__ == "__main__":
    unittest.main()
