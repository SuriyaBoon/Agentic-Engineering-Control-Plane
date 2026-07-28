from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .agents import ReviewerAgent, default_agents
from .context import ContextBuilder
from .github_client import GitHubAccessError, GitHubClient
from .inventory import inventory_payload
from .models import AuditReport, RepositoryDescriptor
from .policy import AuditPolicy
from .provenance import git_commit_sha, source_tree_sha256
from .reporting import EvidenceWriter


class AuditOrchestrator:
    def __init__(
        self,
        *,
        policy: AuditPolicy,
        runtime_root: str | Path,
        github_client: GitHubClient | None = None,
    ) -> None:
        self.policy = policy
        self.runtime_root = Path(runtime_root)
        self.github = github_client
        self.context_builder = ContextBuilder(policy)
        enabled = set(policy.enabled_agents)
        self.agents = [agent for agent in default_agents() if agent.name in enabled]
        self.reviewer = ReviewerAgent()

    @staticmethod
    def _local_source(
        repository: RepositoryDescriptor,
        roots: Iterable[str | Path],
    ) -> Path | None:
        for raw_root in roots:
            root = Path(raw_root)
            if root.is_dir() and root.name.lower() == repository.name.lower():
                return root.resolve()
            if root.is_dir():
                direct = root / repository.name
                if direct.is_dir():
                    return direct.resolve()
                for child in root.iterdir():
                    if child.is_dir() and child.name.lower() == repository.name.lower():
                        return child.resolve()
        return None

    def audit_repository(
        self,
        repository: RepositoryDescriptor,
        source_path: str | Path,
    ) -> AuditReport:
        context = self.context_builder.build(repository, source_path)
        source_identity = repository.commit_sha or git_commit_sha(context.root)
        if source_identity is None:
            source_identity = source_tree_sha256(context.root, context.files)
        repository = replace(repository, commit_sha=source_identity)
        context.descriptor = repository
        raw_findings = []
        agents_run = []
        for agent in self.agents:
            raw_findings.extend(agent.run(context))
            agents_run.append(agent.name)
        findings, limitations = self.reviewer.review(context, raw_findings)
        agents_run.append(self.reviewer.name)
        return AuditReport(
            repository=repository,
            status="audited",
            source_path=str(context.root),
            agents_run=agents_run,
            findings=findings,
            metrics={
                "file_count": len(context.files),
                "text_file_count": len(context.text_files),
                "source_file_count": len(context.source_files),
                "test_file_count": len(context.test_files),
                "workflow_file_count": len(context.workflow_files),
                "manifest_file_count": len(context.manifest_files),
                "context_truncated": context.truncated,
            },
            limitations=limitations,
            reviewer_passed=True,
        )

    @staticmethod
    def _unavailable_report(
        repository: RepositoryDescriptor,
        *,
        status: str,
        limitation: str,
    ) -> AuditReport:
        return AuditReport(
            repository=repository,
            status=status,
            source_path=None,
            agents_run=[],
            findings=[],
            metrics={
                "file_count": 0,
                "text_file_count": 0,
                "source_file_count": 0,
                "test_file_count": 0,
                "workflow_file_count": 0,
                "manifest_file_count": 0,
                "context_truncated": False,
            },
            limitations=[limitation],
            reviewer_passed=False,
        )

    def audit_all(
        self,
        *,
        owner: str,
        repositories: list[RepositoryDescriptor],
        inventory: dict,
        local_roots: Iterable[str | Path] = (),
        download_missing: bool = False,
    ) -> tuple[Path, dict]:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_root = self.runtime_root / "runs" / run_id
        writer = EvidenceWriter(run_root)
        reports: list[AuditReport] = []

        for repository in repositories:
            source = self._local_source(repository, local_roots)
            descriptor = repository
            if source is None and download_missing:
                if self.github is None:
                    reports.append(
                        self._unavailable_report(
                            repository,
                            status="unavailable",
                            limitation="No GitHub client is configured for snapshot download.",
                        )
                    )
                    writer.write_repository(reports[-1])
                    continue
                try:
                    descriptor, source = self.github.download_snapshot(
                        repository,
                        self.runtime_root / "snapshots",
                    )
                except GitHubAccessError as error:
                    status = (
                        "auth_required"
                        if repository.visibility != "public"
                        else "unavailable"
                    )
                    reports.append(
                        self._unavailable_report(
                            repository,
                            status=status,
                            limitation=str(error),
                        )
                    )
                    writer.write_repository(reports[-1])
                    continue
                except Exception as error:
                    reports.append(
                        self._unavailable_report(
                            repository,
                            status="error",
                            limitation=f"{type(error).__name__}: {error}",
                        )
                    )
                    writer.write_repository(reports[-1])
                    continue

            if source is None:
                status = (
                    "auth_required"
                    if repository.visibility != "public"
                    else "source_missing"
                )
                limitation = (
                    "Private source was not available; authenticate with GITHUB_TOKEN."
                    if status == "auth_required"
                    else "No local source or downloaded snapshot was available."
                )
                report = self._unavailable_report(
                    repository,
                    status=status,
                    limitation=limitation,
                )
            else:
                try:
                    report = self.audit_repository(descriptor, source)
                except Exception as error:
                    report = self._unavailable_report(
                        descriptor,
                        status="error",
                        limitation=f"{type(error).__name__}: {error}",
                    )
            reports.append(report)
            writer.write_repository(report)

        live_inventory = inventory_payload(owner, [item.repository for item in reports], source="audit_run")
        live_inventory["baseline_inventory"] = inventory
        portfolio = writer.finalize(
            owner=owner,
            run_id=run_id,
            reports=reports,
            inventory=live_inventory,
        )
        return run_root, portfolio
