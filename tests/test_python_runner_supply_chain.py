import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "python-runner.Dockerfile"
LOCK = ROOT / "docker" / "python-runner-requirements.txt"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-python-runner.yml"
POLICY = ROOT / "config" / "development-policy.json"
RELEASE_DOC = ROOT / "docs" / "PYTHON-RUNNER-RELEASE.md"


class PythonRunnerSupplyChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        cls.lock = LOCK.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.policy = POLICY.read_text(encoding="utf-8")
        cls.release_doc = RELEASE_DOC.read_text(encoding="utf-8")

    def test_runner_installs_only_hash_locked_binary_dependencies(self):
        requirements = [
            line.split(" ", 1)[0]
            for line in self.lock.splitlines()
            if line and not line.startswith(("#", " "))
        ]
        self.assertIn("pytest==8.4.2", requirements)
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertRegex(requirement, r"^[a-z0-9-]+==[0-9][^ ]*(?: ; .+)?$")
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", self.lock)
        self.assertEqual(len(hashes), len(requirements))
        self.assertEqual(len(hashes), len(set(hashes)))
        for required in (
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
            "--index-url=https://pypi.org/simple",
            "python -m pip check",
            "python -m pytest --version",
            "COPY --chown=0:0 --chmod=0444",
            'PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"',
            "USER 65532:65532",
        ):
            self.assertIn(required, self.dockerfile)

    def test_publication_privilege_follows_qualification(self):
        self.assertRegex(self.workflow, r"(?m)^  workflow_dispatch:\s*$")
        qualify = self.workflow.split("  qualify:", 1)[1].split("  publish:", 1)[0]
        publish = self.workflow.split("  publish:", 1)[1]
        self.assertNotIn("packages: write", qualify)
        self.assertIn("needs: qualify", publish)
        self.assertIn("if: github.ref == 'refs/heads/main'", publish)
        self.assertIn("environment: python-runner-release", publish)
        self.assertIn("packages: write", publish)
        self.assertIn("sha256sum --check runner-image.tar.sha256", publish)
        self.assertIn("docker load --input runner-image.tar", publish)
        self.assertIn("$(cat runner-image.id)", publish)
        self.assertIn("run-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${GITHUB_SHA}", publish)
        self.assertIn("docker buildx imagetools inspect", publish)
        self.assertIn("runner-config.digest", qualify)
        self.assertIn("runner-platform.txt", qualify)
        self.assertIn('IMMUTABLE_REF="$immutable_ref"', publish)
        self.assertIn('"--raw"', publish)
        self.assertIn('document.get("manifests", [])', publish)
        self.assertIn('child.get("config")', publish)
        self.assertIn('remote_config != qualified_config', publish)
        self.assertIn('runner-remote-config.digest', publish)
        self.assertIn('"schema_version": "ae.python_runner_release.v2"', publish)
        self.assertIn('"qualified_config_digest"', publish)
        self.assertIn('"remote_config_digest"', publish)
        self.assertIn('"target_platform"', publish)
        self.assertIn('"policy_activated": False', publish)

    def test_workflow_actions_are_commit_pinned(self):
        uses = re.findall(r"(?m)^\s*- uses: ([^\s]+)$", self.workflow)
        self.assertGreaterEqual(len(uses), 4)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[a-z0-9_.-]+/[a-z0-9_.-]+@[0-9a-f]{40}$")

    def test_runtime_qualification_preserves_phase_zero_controls(self):
        for required in (
            "--network none",
            "--read-only",
            "--user 65532:65532",
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            "--pids-limit 128",
            "--memory 1g",
            "--cpus 1.0",
            "--ipc none",
            'sudo install -d -o 65532 -g "$(id -g)" -m 0750 "$output"',
            "dst=/workspace,readonly",
            "python -m pytest tests -v",
        ):
            self.assertIn(required, self.workflow)

    def test_existing_policy_is_not_activated_before_digest_publication(self):
        self.assertIn("docker.io/library/python@sha256:", self.policy)
        self.assertNotIn("agentic-engineering-python-runner@sha256:", self.policy)

    def test_activation_requires_exact_digest_evidence(self):
        self.assertRegex(self.release_doc, r"required\s+reviewers")
        self.assertIn("activation_reference", self.release_doc)
        self.assertIn("exact digest", self.release_doc)
        self.assertIn("activation evidence", self.release_doc)


if __name__ == "__main__":
    unittest.main()
