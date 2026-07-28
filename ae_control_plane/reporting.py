from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AuditReport, SEVERITY_ORDER


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def report_markdown(report: AuditReport) -> str:
    lines = [
        f"# {report.repository.full_name}",
        "",
        f"- Status: `{report.status}`",
        f"- Visibility: `{report.repository.visibility}`",
        f"- Default branch: `{report.repository.default_branch}`",
        f"- Commit: `{report.repository.commit_sha or 'not captured'}`",
        f"- Reviewer passed: `{str(report.reviewer_passed).lower()}`",
        "",
        "## Metrics",
        "",
    ]
    for key, value in sorted(report.metrics.items()):
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Findings", ""])
    if not report.findings:
        lines.append("No rule-backed findings were produced for the available source.")
    for finding in report.findings:
        lines.extend(
            [
                f"### [{finding.severity.upper()}] {finding.title}",
                "",
                f"- Agent: `{finding.agent}`",
                f"- Category: `{finding.category}`",
                f"- Finding ID: `{finding.finding_id}`",
                f"- Confidence: `{finding.confidence:.2f}`",
                "- Evidence:",
            ]
        )
        lines.extend(f"  - `{item}`" for item in finding.evidence)
        lines.extend(["- Recommendation:", f"  - {finding.recommendation}", ""])
    lines.extend(["## Limitations", ""])
    if report.limitations:
        lines.extend(f"- {item}" for item in report.limitations)
    else:
        lines.append("- None recorded.")
    lines.append("")
    return "\n".join(lines)


def portfolio_payload(
    *,
    owner: str,
    run_id: str,
    reports: list[AuditReport],
) -> dict[str, Any]:
    statuses = Counter(report.status for report in reports)
    severities = Counter(
        finding.severity for report in reports for finding in report.findings
    )
    return {
        "schema_version": "1.0.0",
        "owner": owner,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_count": len(reports),
        "status_counts": dict(sorted(statuses.items())),
        "finding_counts": {
            severity: severities.get(severity, 0)
            for severity in sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get)
        },
        "repositories": [
            {
                "full_name": report.repository.full_name,
                "visibility": report.repository.visibility,
                "status": report.status,
                "finding_count": len(report.findings),
                "reviewer_passed": report.reviewer_passed,
                "limitations": report.limitations,
            }
            for report in reports
        ],
        "production_execution": False,
        "source_repository_mutation": False,
    }


def portfolio_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Agentic Engineering Portfolio Audit: {payload['owner']}",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Repositories: `{payload['repository_count']}`",
        f"- Production execution: `{str(payload['production_execution']).lower()}`",
        f"- Source mutation: `{str(payload['source_repository_mutation']).lower()}`",
        "",
        "## Coverage",
        "",
    ]
    for status, count in payload["status_counts"].items():
        lines.append(f"- {status}: `{count}`")
    lines.extend(["", "## Findings", ""])
    for severity, count in payload["finding_counts"].items():
        lines.append(f"- {severity}: `{count}`")
    lines.extend(
        [
            "",
            "## Repository results",
            "",
            "| Repository | Visibility | Status | Findings | Reviewer |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for item in payload["repositories"]:
        lines.append(
            f"| {item['full_name']} | {item['visibility']} | {item['status']} | "
            f"{item['finding_count']} | {str(item['reviewer_passed']).lower()} |"
        )
    lines.append("")
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EvidenceWriter:
    def __init__(self, run_root: str | Path) -> None:
        self.root = Path(run_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_repository(self, report: AuditReport) -> None:
        safe_name = report.repository.full_name.replace("/", "__")
        _write_json(self.root / "repositories" / f"{safe_name}.json", report.to_dict())
        markdown = self.root / "repositories" / f"{safe_name}.md"
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(report_markdown(report), encoding="utf-8")

    def finalize(
        self,
        *,
        owner: str,
        run_id: str,
        reports: list[AuditReport],
        inventory: dict[str, Any],
    ) -> dict[str, Any]:
        _write_json(self.root / "inventory.json", inventory)
        portfolio = portfolio_payload(owner=owner, run_id=run_id, reports=reports)
        _write_json(self.root / "portfolio.json", portfolio)
        (self.root / "portfolio.md").write_text(
            portfolio_markdown(portfolio),
            encoding="utf-8",
        )
        files = sorted(
            path for path in self.root.rglob("*") if path.is_file() and path.name != "manifest.json"
        )
        manifest = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "algorithm": "sha256",
            "files": [
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
                for path in files
            ],
        }
        _write_json(self.root / "manifest.json", manifest)
        return portfolio
