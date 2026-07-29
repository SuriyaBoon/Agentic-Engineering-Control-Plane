from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ae_control_plane.isolation import DockerTestRunner, IsolationConfig


IMAGE = (
    "docker.io/library/python@sha256:"
    "9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a"
)


def docker_config() -> IsolationConfig:
    return IsolationConfig.from_dict(
        {
            "mode": "docker",
            "docker": {
                "executable": "docker",
                "image": IMAGE,
                "user": "65532:65532",
                "memory": "1g",
                "cpus": "1.0",
                "pids_limit": 128,
                "tmpfs_size": "128m",
            },
        }
    )


class DockerIsolationUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        self.output = self.root / "output"
        self.runner = DockerTestRunner(docker_config())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_policy_rejects_unpinned_root_and_implicit_host_execution(self) -> None:
        with self.assertRaises(ValueError):
            IsolationConfig.from_dict(
                {"mode": "host", "trusted_host_execution": False}
            )
        with self.assertRaises(ValueError):
            IsolationConfig.from_dict(
                {
                    "mode": "docker",
                    "docker": {
                        "image": "python:3.12",
                        "user": "65532:65532",
                        "memory": "1g",
                        "cpus": "1",
                        "pids_limit": 128,
                        "tmpfs_size": "128m",
                    },
                }
            )
        payload = {
            "mode": "docker",
            "docker": {
                "image": IMAGE,
                "user": "0:0",
                "memory": "1g",
                "cpus": "1",
                "pids_limit": 128,
                "tmpfs_size": "128m",
            },
        }
        with self.assertRaises(ValueError):
            IsolationConfig.from_dict(payload)

    def test_command_contains_required_isolation_controls(self) -> None:
        command = self.runner.build_command(
            ["python", "-m", "unittest"],
            workspace=self.workspace,
            output_dir=self.output,
            container_name="ae-test-123456789abc",
        )
        rendered = "\n".join(command)
        for expected in (
            "--network\nnone",
            "--read-only",
            "--user\n65532:65532",
            "--cap-drop\nALL",
            "--security-opt\nno-new-privileges:true",
            "--pids-limit\n128",
            "--memory\n1g",
            "--cpus\n1.0",
            "--ipc\nnone",
            "dst=/workspace,readonly",
            "dst=/output",
            "PYTHONDONTWRITEBYTECODE=1",
            IMAGE,
        ):
            self.assertIn(expected, rendered)

    def test_unsupported_runtime_and_unsafe_paths_fail_closed(self) -> None:
        with self.assertRaises(PermissionError):
            self.runner.build_command(
                ["powershell", "-File", "test.ps1"],
                workspace=self.workspace,
                output_dir=self.output,
                container_name="ae-test-123456789abc",
            )
        with self.assertRaises(PermissionError):
            self.runner.build_command(
                ["python", "-V"],
                workspace=self.root / "missing",
                output_dir=self.output,
                container_name="ae-test-123456789abc",
            )
        with self.assertRaises(PermissionError):
            self.runner.build_command(
                ["python", "-V"],
                workspace=self.workspace,
                output_dir=self.workspace / "output",
                container_name="ae-test-123456789abc",
            )
        comma_workspace = self.root / "work,space"
        comma_workspace.mkdir()
        (comma_workspace / ".git").mkdir()
        with self.assertRaises(PermissionError):
            self.runner.build_command(
                ["python", "-V"],
                workspace=comma_workspace,
                output_dir=self.output,
                container_name="ae-test-123456789abc",
            )

    @patch("ae_control_plane.isolation.subprocess.run")
    def test_unavailable_daemon_and_timeout_fail_closed_with_cleanup(
        self, run: unittest.mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["docker"], 1, "", "unavailable"
        )
        with self.assertRaises(RuntimeError):
            self.runner.ensure_available()

        run.side_effect = [
            subprocess.CompletedProcess(["docker"], 0, "29.0\n", ""),
            subprocess.CompletedProcess(["docker"], 0, "[]", ""),
            subprocess.TimeoutExpired(["docker", "run"], 1),
            subprocess.CompletedProcess(["docker"], 0, "", ""),
        ]
        with patch(
            "ae_control_plane.isolation.uuid.uuid4",
            return_value=type("Uuid", (), {"hex": "123456789abcdef"})(),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.runner.run(
                    ["python", "-V"],
                    workspace=self.workspace,
                    output_dir=self.output,
                    timeout=1,
                )
        cleanup = run.call_args_list[-1].args[0]
        self.assertEqual(
            cleanup,
            ["docker", "rm", "--force", "ae-test-123456789abc"],
        )


@unittest.skipUnless(
    os.environ.get("AE_RUN_DOCKER_TESTS") == "1",
    "set AE_RUN_DOCKER_TESTS=1 for live Docker isolation tests",
)
class DockerIsolationLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.workspace,
            capture_output=True,
            check=True,
            text=True,
        )
        self.output = self.root / "output"
        self.runner = DockerTestRunner(docker_config())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_python(self, source: str, timeout: int = 20) -> str:
        result = self.runner.run(
            ["python", "-c", source],
            workspace=self.workspace,
            output_dir=self.output,
            timeout=timeout,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_non_root_read_only_source_network_none_and_writable_output(
        self,
    ) -> None:
        result = self.run_python(
            "import os,socket,pathlib;"
            "assert os.getuid()!=0;"
            "\ntry: pathlib.Path('/workspace/escape').write_text('bad')"
            "\nexcept OSError: pass"
            "\nelse: raise AssertionError('workspace writable');"
            "\ntry: socket.create_connection(('1.1.1.1',53),.3)"
            "\nexcept OSError: pass"
            "\nelse: raise AssertionError('network available');"
            "\npathlib.Path(os.environ['AE_OUTPUT_DIR'],'proof.txt').write_text('ok')"
            "\nprint(os.getuid())"
        )
        self.assertNotEqual(result, "0")
        self.assertFalse((self.workspace / "escape").exists())
        self.assertEqual(
            (self.output / "proof.txt").read_text(encoding="utf-8"),
            "ok",
        )

    def test_timeout_removes_container(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.runner.run(
                ["python", "-c", "import time; time.sleep(10)"],
                workspace=self.workspace,
                output_dir=self.output,
                timeout=1,
            )
        remaining = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "name=ae-test-",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        self.assertEqual(remaining.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
