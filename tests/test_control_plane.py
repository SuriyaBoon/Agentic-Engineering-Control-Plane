from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from ae_control_plane.agents import ReviewerAgent, SecurityAgent, TestingAgent, finding
from ae_control_plane.context import ContextBuilder
from ae_control_plane.github_client import GitHubAccessError, GitHubClient
from ae_control_plane.inventory import load_inventory, merge_inventory
from ae_control_plane.models import RepositoryDescriptor
from ae_control_plane.orchestrator import AuditOrchestrator
from ae_control_plane.policy import AuditPolicy
from ae_control_plane.provenance import source_tree_sha256


ROOT = Path(__file__).parents[1]
POLICY = ROOT / "config" / "policy.json"
INVENTORY = ROOT / "config" / "inventory-snapshot.json"


def descriptor(name: str = "fixture", visibility: str = "public") -> RepositoryDescriptor:
    return RepositoryDescriptor(
        id=1,
        name=name,
        full_name=f"SuriyaBoon/{name}",
        visibility=visibility,
        default_branch="main",
        clone_url=f"https://github.com/SuriyaBoon/{name}.git",
    )


class PolicyTests(unittest.TestCase):
    def test_policy_is_read_only(self) -> None:
        policy = AuditPolicy(POLICY)
        self.assertEqual(policy.config["mode"], "read_only_audit")
        self.assertIs(policy.config["source_repository_mutation"], False)

    def test_mutating_policy_fails_closed(self) -> None:
        payload = json.loads(POLICY.read_text(encoding="utf-8"))
        payload["source_repository_mutation"] = True
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                AuditPolicy(path)


class InventoryTests(unittest.TestCase):
    def test_seed_inventory_has_full_current_snapshot(self) -> None:
        _, repositories = load_inventory(INVENTORY)
        self.assertEqual(len(repositories), 24)
        self.assertEqual(sum(item.visibility == "public" for item in repositories), 20)
        self.assertEqual(sum(item.visibility == "private" for item in repositories), 4)
        self.assertEqual(len({item.full_name.lower() for item in repositories}), 24)

    def test_anonymous_live_inventory_does_not_drop_private_baseline(self) -> None:
        private = descriptor("private", "private")
        public = descriptor("public", "public")
        refreshed = replace(public, default_branch="trunk")
        merged = merge_inventory([private, public], [refreshed])
        self.assertEqual(len(merged), 2)
        by_name = {item.name: item for item in merged}
        self.assertEqual(by_name["public"].default_branch, "trunk")
        self.assertEqual(by_name["private"].visibility, "private")


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AuditPolicy(POLICY)

    def test_security_agent_and_reviewer_use_real_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                'token = "1234567890secret"\n'
                'subprocess.run("whoami", shell=True)\n',
                encoding="utf-8",
            )
            context = ContextBuilder(self.policy).build(descriptor(), root)
            findings = SecurityAgent().run(context)
            accepted, limitations = ReviewerAgent().review(context, findings)
            self.assertGreaterEqual(len(accepted), 2)
            self.assertEqual(limitations, [])
            self.assertTrue(all("app.py" in item.evidence for item in accepted))

    def test_reviewer_rejects_unverifiable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            context = ContextBuilder(self.policy).build(descriptor(), root)
            proposed = finding(
                context,
                agent="test",
                category="integrity",
                severity="high",
                title="Unsupported proposal",
                evidence=["missing.txt"],
                recommendation="Do not accept.",
            )
            accepted, limitations = ReviewerAgent().review(context, [proposed])
            self.assertEqual(accepted, [])
            self.assertEqual(len(limitations), 1)

    def test_source_without_tests_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            context = ContextBuilder(self.policy).build(descriptor(), root)
            titles = {item.title for item in TestingAgent().run(context)}
            self.assertIn("Executable source has no detected automated tests", titles)


class ProvenanceTests(unittest.TestCase):
    def test_tree_hash_is_stable_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "a.txt"
            path.write_text("one", encoding="utf-8")
            first = source_tree_sha256(root, ("a.txt",))
            second = source_tree_sha256(root, ("a.txt",))
            self.assertEqual(first, second)
            path.write_text("two", encoding="utf-8")
            self.assertNotEqual(first, source_tree_sha256(root, ("a.txt",)))


class SnapshotSafetyTests(unittest.TestCase):
    def test_unsafe_zip_path_is_rejected(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("root/../escape.txt", "bad")
        with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as archive:
            with self.assertRaises(ValueError):
                GitHubClient._safe_members(archive)

    def test_private_snapshot_requires_authentication(self) -> None:
        client = GitHubClient(token=None)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(GitHubAccessError):
                client.download_snapshot(descriptor("private", "private"), temporary)


class OrchestratorTests(unittest.TestCase):
    def test_audit_run_writes_complete_evidence(self) -> None:
        policy = AuditPolicy(POLICY)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "fixture"
            source.mkdir()
            (source / "README.md").write_text(
                "# Fixture\n\nLimitations: lab only.\nTesting: unittest.\n",
                encoding="utf-8",
            )
            (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "tests").mkdir()
            (source / "tests" / "test_app.py").write_text(
                "def test_invalid_input_is_rejected():\n    assert True\n",
                encoding="utf-8",
            )
            repo = descriptor()
            inventory = {
                "schema_version": "1.0.0",
                "owner": "SuriyaBoon",
                "generated_at": "2026-07-28T00:00:00+00:00",
                "source": "test",
                "repositories": [repo.to_dict()],
            }
            orchestrator = AuditOrchestrator(
                policy=policy,
                runtime_root=base / "runtime",
            )
            run_root, portfolio = orchestrator.audit_all(
                owner="SuriyaBoon",
                repositories=[repo],
                inventory=inventory,
                local_roots=[base],
            )
            self.assertEqual(portfolio["status_counts"], {"audited": 1})
            report_path = run_root / "repositories" / "SuriyaBoon__fixture.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["reviewer_passed"])
            self.assertTrue(report["repository"]["commit_sha"].startswith("tree-sha256:"))
            manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
            manifest_paths = {item["path"] for item in manifest["files"]}
            self.assertIn("portfolio.json", manifest_paths)
            self.assertIn("repositories/SuriyaBoon__fixture.json", manifest_paths)

    def test_missing_private_source_is_not_silently_skipped(self) -> None:
        policy = AuditPolicy(POLICY)
        with tempfile.TemporaryDirectory() as temporary:
            repo = descriptor("private", "private")
            inventory = {
                "schema_version": "1.0.0",
                "owner": "SuriyaBoon",
                "generated_at": "2026-07-28T00:00:00+00:00",
                "source": "test",
                "repositories": [repo.to_dict()],
            }
            orchestrator = AuditOrchestrator(
                policy=policy,
                runtime_root=Path(temporary) / "runtime",
            )
            _, portfolio = orchestrator.audit_all(
                owner="SuriyaBoon",
                repositories=[repo],
                inventory=inventory,
            )
            self.assertEqual(portfolio["status_counts"], {"auth_required": 1})


if __name__ == "__main__":
    unittest.main()
