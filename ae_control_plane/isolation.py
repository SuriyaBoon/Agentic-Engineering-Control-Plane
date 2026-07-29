from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PINNED_IMAGE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)
PYTHON_EXECUTABLES = {"python", "python.exe"}


@dataclass(frozen=True)
class IsolationConfig:
    mode: str
    trusted_host_execution: bool = False
    docker_executable: str = "docker"
    image: str = ""
    user: str = "65532:65532"
    memory: str = "1g"
    cpus: str = "1.0"
    pids_limit: int = 128
    tmpfs_size: str = "128m"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IsolationConfig":
        if not isinstance(payload, dict):
            raise ValueError("test_execution policy is required")
        mode = str(payload.get("mode", ""))
        if mode == "host":
            if payload.get("trusted_host_execution") is not True:
                raise ValueError(
                    "host tests require explicit trusted_host_execution"
                )
            return cls(mode="host", trusted_host_execution=True)
        if mode != "docker":
            raise ValueError("test execution mode must be host or docker")
        docker = payload.get("docker")
        if not isinstance(docker, dict):
            raise ValueError("docker isolation settings are required")
        executable = str(docker.get("executable", "docker"))
        if Path(executable).name.lower() not in {"docker", "docker.exe"}:
            raise ValueError("docker executable must be docker or docker.exe")
        image = str(docker.get("image", ""))
        if not PINNED_IMAGE.fullmatch(image):
            raise ValueError("docker image must use an immutable sha256 digest")
        user = str(docker.get("user", ""))
        if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user):
            raise ValueError("docker user must be a non-root numeric uid:gid")
        memory = str(docker.get("memory", ""))
        cpus = str(docker.get("cpus", ""))
        tmpfs_size = str(docker.get("tmpfs_size", ""))
        if not re.fullmatch(r"[1-9][0-9]*[kmg]", memory):
            raise ValueError("docker memory limit is invalid")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", cpus) or float(cpus) <= 0:
            raise ValueError("docker CPU limit is invalid")
        if not re.fullmatch(r"[1-9][0-9]*[kmg]", tmpfs_size):
            raise ValueError("docker tmpfs limit is invalid")
        pids_limit = int(docker.get("pids_limit", 0))
        if pids_limit < 16 or pids_limit > 4096:
            raise ValueError("docker PID limit must be between 16 and 4096")
        return cls(
            mode="docker",
            docker_executable=executable,
            image=image,
            user=user,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            tmpfs_size=tmpfs_size,
        )


class DockerTestRunner:
    def __init__(self, config: IsolationConfig) -> None:
        if config.mode != "docker":
            raise ValueError("DockerTestRunner requires docker mode")
        self.config = config

    @staticmethod
    def _validate_command(argv: list[str]) -> list[str]:
        if not isinstance(argv, list) or not argv or len(argv) > 64:
            raise ValueError("test command must contain 1 to 64 arguments")
        resolved = [str(part) for part in argv]
        if any(
            not part or "\x00" in part or len(part) > 4096
            for part in resolved
        ):
            raise ValueError("test command contains an invalid argument")
        executable = Path(resolved[0]).name.lower()
        if executable not in PYTHON_EXECUTABLES:
            raise PermissionError(
                f"docker runner does not support executable: {executable}"
            )
        resolved[0] = "python"
        return resolved

    @staticmethod
    def _validate_paths(
        workspace: Path, output_dir: Path
    ) -> tuple[Path, Path]:
        if workspace.is_symlink() or not workspace.is_dir():
            raise PermissionError("workspace must be a real directory")
        source = workspace.resolve()
        git_dir = source / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise PermissionError("workspace must be an isolated Git clone")
        output_parent = output_dir.parent
        if output_parent.is_symlink() or not output_parent.is_dir():
            raise PermissionError("container output parent must be a real directory")
        output_dir.mkdir(exist_ok=True)
        if output_dir.is_symlink():
            raise PermissionError("container output must not be a symlink")
        output = output_dir.resolve()
        if output == source or source in output.parents:
            raise PermissionError(
                "container output must be outside the read-only workspace"
            )
        if any(
            "," in str(path) or "\n" in str(path) or "\r" in str(path)
            for path in (source, output)
        ):
            raise PermissionError("container mount path contains unsafe characters")
        return source, output

    def ensure_available(self) -> str:
        try:
            version = subprocess.run(
                [
                    self.config.docker_executable,
                    "version",
                    "--format",
                    "{{.Server.Version}}",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Docker daemon is unavailable") from exc
        if version.returncode or not version.stdout.strip():
            raise RuntimeError("Docker daemon is unavailable")
        try:
            image = subprocess.run(
                [
                    self.config.docker_executable,
                    "image",
                    "inspect",
                    self.config.image,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                "digest-pinned Docker test image is unavailable locally"
            ) from exc
        if image.returncode:
            raise RuntimeError(
                "digest-pinned Docker test image is unavailable locally"
            )
        return version.stdout.strip()

    def build_command(
        self,
        argv: list[str],
        *,
        workspace: Path,
        output_dir: Path,
        container_name: str,
    ) -> list[str]:
        source, output = self._validate_paths(workspace, output_dir)
        command = self._validate_command(argv)
        if not re.fullmatch(r"ae-test-[a-f0-9]{12}", container_name):
            raise ValueError("invalid test container name")
        uid, gid = self.config.user.split(":", 1)
        return [
            self.config.docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--user",
            self.config.user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(self.config.pids_limit),
            "--memory",
            self.config.memory,
            "--cpus",
            self.config.cpus,
            "--ipc",
            "none",
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,"
                f"size={self.config.tmpfs_size},uid={uid},gid={gid}"
            ),
            "--mount",
            f"type=bind,src={source},dst=/workspace,readonly",
            "--mount",
            f"type=bind,src={output},dst=/output",
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "AE_OUTPUT_DIR=/output",
            self.config.image,
            *command,
        ]

    def run(
        self,
        argv: list[str],
        *,
        workspace: Path,
        output_dir: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.ensure_available()
        name = f"ae-test-{uuid.uuid4().hex[:12]}"
        command = self.build_command(
            argv,
            workspace=workspace,
            output_dir=output_dir,
            container_name=name,
        )
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except OSError as exc:
            raise RuntimeError("isolated test failed to start") from exc
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                [self.config.docker_executable, "rm", "--force", name],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            raise RuntimeError(
                f"isolated test timed out after {timeout} seconds"
            ) from exc
