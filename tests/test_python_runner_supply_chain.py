import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "python-runner.Dockerfile"
LOCK = ROOT / "docker" / "python-runner-requirements.txt"
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-python-runner.yml"
POLICY = ROOT / "config" / "development-policy.json"
QUALIFICATION_DOC = ROOT / "docs" / "PYTHON-RUNNER-QUALIFICATION.md"


class PythonRunnerSupplyChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        cls.lock = LOCK.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.policy = POLICY.read_text(encoding="utf-8")
        cls.qualification_doc = QUALIFICATION_DOC.read_text(encoding="utf-8")

    def test_runner_installs_only_hash_locked_binary_dependencies(self):
        requirements = [
            line.split(" ", 1)[0]
            for line in self.lock.splitlines()
            if line and not line.startswith(("#", " "))
        ]
        self.assertIn("pytest==9.0.3", requirements)
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

    def test_no_known_vulnerable_pytest_pin_remains(self):
        self.assertNotIn("pytest==8.4.2", self.lock)
        pinned = re.search(r"^pytest==([0-9][^\s]*)", self.lock, re.MULTILINE)
        self.assertIsNotNone(pinned, "no pytest pin found in lock file")
        version = tuple(int(part) for part in pinned.group(1).split(".")[:3])
        self.assertGreaterEqual(version, (9, 0, 3))

    def test_workflow_is_qualification_only(self):
        self.assertRegex(self.workflow, r"(?m)^  workflow_dispatch:\s*$")
        self.assertIn("  qualify:", self.workflow)
        for forbidden in (
            "  publish:",
            "packages: write",
            "docker/login-action",
            "docker push",
            "ghcr.io",
            "environment:",
            "deployment",
            "activation_reference",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.workflow)
        self.assertNotRegex(
            self.workflow,
            r"(?m)^\s+[a-z-]+:\s+write\s*$",
        )

    def test_qualification_bundle_preserves_exact_image_evidence(self):
        for required in (
            "docker save --output runner-image.tar",
            "sha256sum runner-image.tar > runner-image.tar.sha256",
            "runner-image.id",
            "runner-config.digest",
            "runner-platform.txt",
            "python-runner-qualification.json",
            '"schema_version": "ae.python_runner_qualification.v1"',
            '"qualified_archive_sha256"',
            '"qualified_config_digest"',
            '"target_platform"',
            '"runtime_network_required": False',
            '"publication_performed": False',
            '"policy_activated": False',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.workflow)

    def test_qualification_artifact_identity_is_retry_stable(self):
        stable = "qualified-python-runner-${{ github.run_id }}-${{ github.sha }}"
        self.assertIn(stable, self.workflow)
        self.assertNotIn("github.run_attempt", self.workflow)
        self.assertIn("overwrite: true", self.workflow)
        self.assertEqual(self.workflow.count(stable), 1)

    def test_workflow_actions_are_commit_pinned(self):
        uses = re.findall(r"(?m)^\s*- uses: ([^\s]+)$", self.workflow)
        self.assertGreaterEqual(len(uses), 2)
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

    def test_existing_policy_is_not_activated_by_qualification(self):
        self.assertIn("docker.io/library/python@sha256:", self.policy)
        self.assertNotIn("agentic-engineering-python-runner@sha256:", self.policy)

    def test_documented_external_boundaries_require_separate_human_control(self):
        for required in (
            "human-owned release process outside this repository",
            "immutable registry digest",
            "remote config digest equals the qualified config digest",
            "separate governed activation task",
        ):
            self.assertIn(required, self.qualification_doc)
        self.assertRegex(
            self.qualification_doc,
            r"Qualification alone never authorizes publication\s+or activation",
        )


if __name__ == "__main__":
    unittest.main()
