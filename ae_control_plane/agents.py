from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from pathlib import Path

from .models import Finding, RepositoryContext, SEVERITY_ORDER


def _finding_id(context: RepositoryContext, agent: str, title: str) -> str:
    identity = f"{context.descriptor.full_name}|{agent}|{title}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
    return f"AE-{agent[:4].upper()}-{suffix}"


def finding(
    context: RepositoryContext,
    *,
    agent: str,
    category: str,
    severity: str,
    title: str,
    evidence: list[str],
    recommendation: str,
    confidence: float = 1.0,
) -> Finding:
    return Finding(
        finding_id=_finding_id(context, agent, title),
        agent=agent,
        category=category,
        severity=severity,
        title=title,
        evidence=tuple(evidence),
        recommendation=recommendation,
        confidence=confidence,
    )


class AuditAgent(ABC):
    name: str

    @abstractmethod
    def run(self, context: RepositoryContext) -> list[Finding]:
        raise NotImplementedError


class ArchitectureAgent(AuditAgent):
    name = "architecture"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        readmes = context.matching_files("readme")
        architecture_evidence = context.search(
            "architecture",
            "data flow",
            "sequenceDiagram",
            "flowchart",
            "stateDiagram",
        )
        if not readmes:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="architecture",
                    severity="medium",
                    title="Repository has no discoverable README",
                    evidence=["metadata:no_readme"],
                    recommendation="Document purpose, entrypoints, boundaries, and execution flow.",
                )
            )
        elif not architecture_evidence and context.source_files:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="architecture",
                    severity="low",
                    title="Architecture and execution flow are not documented",
                    evidence=[readmes[0]],
                    recommendation="Add a source-grounded architecture and data-flow section.",
                )
            )
        if not context.source_files:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="architecture",
                    severity="info",
                    title="No executable source files were detected",
                    evidence=["metadata:source_file_count=0"],
                    recommendation="Classify the repository as documentation, configuration, or unavailable source.",
                )
            )
        return findings


class WorkflowAgent(AuditAgent):
    name = "workflow"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        lifecycle = context.search(
            "state machine",
            "stateDiagram",
            "pending_approval",
            "verification",
            "workflow",
        )
        operational = context.search(
            "New-ADUser",
            "Stop-VM",
            "Start-WBSystemStateRecovery",
            "Invoke-RestMethod",
            "subprocess.run",
        )
        if operational and not lifecycle:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="workflow",
                    severity="medium",
                    title="Operational actions are not wrapped in a documented governed workflow",
                    evidence=operational[:5],
                    recommendation="Define validation, approval, execution, verification, failure, and evidence states.",
                )
            )
        if lifecycle and not context.search("retry", "fallback", "manual_review", "rollback"):
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="workflow",
                    severity="low",
                    title="Workflow recovery and fallback behavior are not explicit",
                    evidence=lifecycle[:5],
                    recommendation="Document bounded retries, manual review, compensation, and terminal failure states.",
                )
            )
        return findings


class SecurityAgent(AuditAgent):
    name = "security"

    SECRET_PATTERNS = (
        re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[\"'][^\"']{8,}[\"']"),
        re.compile(r"(?i)password\s*[:=]\s*[\"'][^\"']{4,}[\"']"),
    )

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        conflicts = []
        possible_secrets = []
        unsafe_execution = []
        live_actions = []
        guarded_live_actions = []

        for path, content in context.text_files.items():
            if "<<<<<<<" in content or ">>>>>>>" in content:
                conflicts.append(path)
            if any(pattern.search(content) for pattern in self.SECRET_PATTERNS):
                possible_secrets.append(path)
            if "shell=True" in content or re.search(r"\beval\s*\(", content):
                unsafe_execution.append(path)
            if any(
                marker.lower() in content.lower()
                for marker in (
                    "New-ADUser",
                    "Stop-VM",
                    "Start-WBSystemStateRecovery",
                    "Remove-ADGroupMember",
                    "Disable-ADAccount",
                )
            ):
                live_actions.append(path)
                if any(
                    guard.lower() in content.lower()
                    for guard in ("-whatif", "dryrun", "dry-run", "shouldprocess", "-confirm")
                ):
                    guarded_live_actions.append(path)

        if conflicts:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="repository_integrity",
                    severity="high",
                    title="Unresolved merge-conflict markers are present",
                    evidence=sorted(set(conflicts)),
                    recommendation="Resolve the conflict and validate the final source before execution.",
                )
            )
        if possible_secrets:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="secret_handling",
                    severity="high",
                    title="Possible hard-coded secret or password material was detected",
                    evidence=sorted(set(possible_secrets))[:10],
                    recommendation="Replace embedded values with secure runtime input or a managed secret reference.",
                    confidence=0.8,
                )
            )
        if unsafe_execution:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="code_execution",
                    severity="high",
                    title="Potentially unsafe dynamic or shell execution was detected",
                    evidence=sorted(set(unsafe_execution)),
                    recommendation="Use argument arrays, strict allowlists, and a sandboxed executor.",
                )
            )
        unguarded = sorted(set(live_actions) - set(guarded_live_actions))
        if unguarded:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="live_action_boundary",
                    severity="high",
                    title="Live infrastructure actions lack an obvious dry-run or confirmation guard",
                    evidence=unguarded,
                    recommendation="Fail closed by default and require a reviewed plan plus explicit lab approval.",
                    confidence=0.85,
                )
            )
        if ".env" in {path.lower() for path in context.files}:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="secret_handling",
                    severity="high",
                    title="A tracked .env file is present",
                    evidence=[".env"],
                    recommendation="Remove the tracked file, rotate exposed values, and keep only a sanitized example.",
                )
            )
        return findings


class TestingAgent(AuditAgent):
    name = "testing"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        if context.source_files and not context.test_files:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="testing",
                    severity="medium",
                    title="Executable source has no detected automated tests",
                    evidence=[
                        f"metadata:source_files={len(context.source_files)}",
                        "metadata:test_files=0",
                    ],
                    recommendation="Add focused unit tests and negative controls for risky behavior.",
                )
            )
        elif context.test_files:
            negative_markers = context.search(
                "raises",
                "reject",
                "denied",
                "invalid",
                "unauthor",
                "tamper",
                "replay",
            )
            if not negative_markers:
                findings.append(
                    finding(
                        context,
                        agent=self.name,
                        category="testing",
                        severity="low",
                        title="Tests were found but negative-control coverage is not evident",
                        evidence=list(context.test_files[:10]),
                        recommendation="Add failure-path, authorization, replay, and malformed-input tests.",
                        confidence=0.75,
                    )
                )
        return findings


class CIAgent(AuditAgent):
    name = "ci"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        if context.source_files and not context.workflow_files:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="continuous_integration",
                    severity="medium",
                    title="Executable source has no detected GitHub Actions workflow",
                    evidence=[
                        f"metadata:source_files={len(context.source_files)}",
                        "metadata:workflow_files=0",
                    ],
                    recommendation="Add least-privilege CI for syntax, tests, and repository hygiene.",
                )
            )
        for workflow in context.workflow_files:
            content = context.text_files.get(workflow, "")
            if "permissions:" not in content:
                findings.append(
                    finding(
                        context,
                        agent=self.name,
                        category="continuous_integration",
                        severity="low",
                        title="GitHub Actions workflow does not declare explicit permissions",
                        evidence=[workflow],
                        recommendation="Declare least-privilege workflow permissions, normally contents: read.",
                    )
                )
        return findings


class DocumentationAgent(AuditAgent):
    name = "documentation"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        readme_paths = [
            path for path in context.files if Path(path).name.lower() == "readme.md"
        ]
        if not readme_paths:
            return findings
        primary = min(readme_paths, key=lambda item: (item.count("/"), len(item)))
        readme = context.text_files.get(primary, "")
        if context.source_files and not re.search(
            r"(?i)\b(test|testing|validation|verify)\b", readme
        ):
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="documentation",
                    severity="low",
                    title="README does not provide a validation or testing path",
                    evidence=[primary],
                    recommendation="Add exact reproducible validation commands and evidence boundaries.",
                )
            )
        if re.search(r"(?i)\b(production[- ]ready|enterprise[- ]grade)\b", readme):
            if not context.test_files or not context.workflow_files:
                findings.append(
                    finding(
                        context,
                        agent=self.name,
                        category="claim_integrity",
                        severity="medium",
                        title="Production or enterprise claim is not backed by tests and CI",
                        evidence=[primary],
                        recommendation="Use lab or concept scope language until production controls are verified.",
                    )
                )
        if not re.search(
            r"(?i)\b(limitations?|not production|safety model|production boundary)\b",
            readme,
        ):
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="documentation",
                    severity="low",
                    title="README does not state an explicit safety or production boundary",
                    evidence=[primary],
                    recommendation="Document what the repository does not claim and which actions are simulated.",
                )
            )
        return findings


class IntegrationAgent(AuditAgent):
    name = "integration"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        integration_claims = context.search(
            "SentinelGRC",
            "connector",
            "integration",
            "evidence bridge",
        )
        contract_files = context.matching_files(
            "schema",
            "contract",
            "connector",
            "adapter",
        )
        if integration_claims and not contract_files:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="integration",
                    severity="low",
                    title="Integration is described without a discoverable versioned contract",
                    evidence=integration_claims[:10],
                    recommendation="Add a versioned schema, producer fixture, and consumer contract test.",
                    confidence=0.8,
                )
            )
        return findings


class GovernanceAgent(AuditAgent):
    name = "governance"

    def run(self, context: RepositoryContext) -> list[Finding]:
        findings: list[Finding] = []
        actions = context.search(
            "New-ADUser",
            "Stop-VM",
            "restore",
            "disable account",
            "isolate",
            "resolve ticket",
        )
        governance = context.search(
            "approval",
            "approver",
            "verification",
            "evidence",
            "risk owner",
            "audit",
        )
        if actions and not governance:
            findings.append(
                finding(
                    context,
                    agent=self.name,
                    category="governance",
                    severity="medium",
                    title="Operational behavior lacks documented ownership, approval, and verification",
                    evidence=actions[:10],
                    recommendation="Add accountable owner, approval, evidence, independent verification, and closure.",
                )
            )
        return findings


class ReviewerAgent:
    name = "reviewer"

    @staticmethod
    def _valid_evidence(context: RepositoryContext, item: str) -> bool:
        if item.startswith("metadata:"):
            return True
        normalized = item.replace("\\", "/").lower()
        return any(path.lower() == normalized for path in context.files)

    def review(
        self,
        context: RepositoryContext,
        findings: list[Finding],
    ) -> tuple[list[Finding], list[str]]:
        reviewed: list[Finding] = []
        limitations: list[str] = []
        seen: set[tuple[str, str]] = set()
        for item in sorted(
            findings,
            key=lambda value: (
                SEVERITY_ORDER[value.severity],
                value.category,
                value.title,
            ),
        ):
            identity = (item.category, item.title)
            if identity in seen:
                continue
            invalid = [
                evidence
                for evidence in item.evidence
                if not self._valid_evidence(context, evidence)
            ]
            if invalid:
                limitations.append(
                    f"Reviewer dropped {item.finding_id}: invalid evidence {invalid}"
                )
                continue
            seen.add(identity)
            reviewed.append(item)
        if context.truncated:
            limitations.append("Repository context reached a configured scan limit.")
        return reviewed, limitations


def default_agents() -> list[AuditAgent]:
    return [
        ArchitectureAgent(),
        WorkflowAgent(),
        SecurityAgent(),
        TestingAgent(),
        CIAgent(),
        DocumentationAgent(),
        IntegrationAgent(),
        GovernanceAgent(),
    ]
