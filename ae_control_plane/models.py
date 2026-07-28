from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass(frozen=True)
class RepositoryDescriptor:
    id: int
    name: str
    full_name: str
    visibility: str
    default_branch: str
    clone_url: str
    archived: bool = False
    commit_sha: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepositoryDescriptor":
        required = (
            "id",
            "name",
            "full_name",
            "visibility",
            "default_branch",
            "clone_url",
        )
        missing = [field for field in required if field not in data]
        if missing:
            raise ValueError(f"repository descriptor missing: {', '.join(missing)}")
        visibility = str(data["visibility"]).lower()
        if visibility not in {"public", "private", "internal"}:
            raise ValueError(f"unsupported visibility: {visibility}")
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            full_name=str(data["full_name"]),
            visibility=visibility,
            default_branch=str(data["default_branch"]),
            clone_url=str(data["clone_url"]),
            archived=bool(data.get("archived", False)),
            commit_sha=(
                str(data["commit_sha"]) if data.get("commit_sha") is not None else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Finding:
    finding_id: str
    agent: str
    category: str
    severity: str
    title: str
    evidence: tuple[str, ...]
    recommendation: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unsupported severity: {self.severity}")
        if not self.evidence:
            raise ValueError("finding evidence is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("finding confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence"] = list(self.evidence)
        return result


@dataclass
class RepositoryContext:
    descriptor: RepositoryDescriptor
    root: Path
    files: tuple[str, ...]
    text_files: dict[str, str]
    extensions: dict[str, int]
    test_files: tuple[str, ...]
    workflow_files: tuple[str, ...]
    manifest_files: tuple[str, ...]
    source_files: tuple[str, ...]
    truncated: bool = False

    def has_file(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return any(item.lower() == normalized for item in self.files)

    def matching_files(self, *needles: str) -> list[str]:
        lowered = tuple(needle.lower() for needle in needles)
        return [
            path
            for path in self.files
            if any(needle in path.lower() for needle in lowered)
        ]

    def search(self, *needles: str) -> list[str]:
        lowered = tuple(needle.lower() for needle in needles)
        matches = []
        for path, content in self.text_files.items():
            value = content.lower()
            if any(needle in value for needle in lowered):
                matches.append(path)
        return matches


@dataclass
class AuditReport:
    repository: RepositoryDescriptor
    status: str
    agents_run: list[str]
    findings: list[Finding]
    metrics: dict[str, Any]
    source_path: str | None = None
    limitations: list[str] = field(default_factory=list)
    reviewer_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository.to_dict(),
            "status": self.status,
            "source_path": self.source_path,
            "agents_run": self.agents_run,
            "findings": [finding.to_dict() for finding in self.findings],
            "metrics": self.metrics,
            "limitations": self.limitations,
            "reviewer_passed": self.reviewer_passed,
        }
