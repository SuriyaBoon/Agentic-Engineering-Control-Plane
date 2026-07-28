from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AuditPolicy:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.config: dict[str, Any] = json.loads(
            self.path.read_text(encoding="utf-8")
        )
        self._validate()

    def _validate(self) -> None:
        if self.config.get("mode") != "read_only_audit":
            raise ValueError("control plane policy must use read_only_audit mode")
        if self.config.get("source_repository_mutation") is not False:
            raise ValueError("source_repository_mutation must be false")
        if self.config.get("production_actions_enabled") is True:
            raise ValueError("production actions cannot be enabled")
        if not self.config.get("agents"):
            raise ValueError("policy must enable at least one agent")

    @property
    def enabled_agents(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.config["agents"])

    @property
    def max_repository_files(self) -> int:
        return int(self.config["runtime"]["max_repository_files"])

    @property
    def max_text_file_bytes(self) -> int:
        return int(self.config["runtime"]["max_text_file_bytes"])

    @property
    def max_total_text_bytes(self) -> int:
        return int(self.config["runtime"]["max_total_text_bytes"])

    @property
    def max_download_bytes(self) -> int:
        return int(self.config["runtime"]["max_download_bytes"])

    @property
    def token_env(self) -> str:
        return str(self.config["network"]["private_repository_token_env"])

    @property
    def workflow_max_retries(self) -> int:
        return int(self.config["workflow"]["max_retries"])

    @property
    def sla_hours(self) -> dict[str, int]:
        return {
            str(key): int(value)
            for key, value in self.config["workflow"]["sla_hours"].items()
        }
