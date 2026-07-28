from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .agents import default_agents
from .github_client import GitHubClient
from .inventory import inventory_payload, load_inventory, merge_inventory, write_inventory
from .models import RepositoryDescriptor
from .orchestrator import AuditOrchestrator
from .policy import AuditPolicy


REPOSITORY_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPOSITORY_ROOT / "config" / "policy.json"
DEFAULT_INVENTORY = REPOSITORY_ROOT / "config" / "inventory-snapshot.json"
DEFAULT_RUNTIME = (
    Path(tempfile.gettempdir()) / "agentic-engineering-control-plane"
)


def _policy(args: argparse.Namespace) -> AuditPolicy:
    return AuditPolicy(args.policy)


def _client(policy: AuditPolicy) -> GitHubClient:
    return GitHubClient.from_environment(
        policy.token_env,
        max_download_bytes=policy.max_download_bytes,
    )


def command_agents(args: argparse.Namespace) -> None:
    policy = _policy(args)
    enabled = set(policy.enabled_agents)
    payload = {
        "mode": policy.config["mode"],
        "agents": [
            {"name": agent.name, "enabled": agent.name in enabled}
            for agent in default_agents()
        ]
        + [{"name": "reviewer", "enabled": "reviewer" in enabled}],
    }
    print(json.dumps(payload, indent=2))


def command_doctor(args: argparse.Namespace) -> None:
    policy = _policy(args)
    snapshot, repositories = load_inventory(args.inventory)
    public = sum(item.visibility == "public" for item in repositories)
    private = len(repositories) - public
    payload = {
        "status": "ready",
        "python": sys.version.split()[0],
        "policy_mode": policy.config["mode"],
        "source_repository_mutation": policy.config["source_repository_mutation"],
        "inventory_owner": snapshot["owner"],
        "repositories": len(repositories),
        "public_repositories": public,
        "private_repositories": private,
        "github_token_present": bool(os.environ.get(policy.token_env)),
        "runtime": str(Path(args.runtime).resolve()),
    }
    if private and not payload["github_token_present"]:
        payload["status"] = "ready_public_auth_required_private"
    print(json.dumps(payload, indent=2))


def command_inventory(args: argparse.Namespace) -> None:
    policy = _policy(args)
    repositories = _client(policy).list_repositories(args.owner)
    payload = inventory_payload(args.owner, repositories, source="github_api")
    if args.output:
        write_inventory(args.output, payload)
    print(json.dumps(payload, indent=2))


def command_audit_repo(args: argparse.Namespace) -> None:
    policy = _policy(args)
    descriptor = RepositoryDescriptor(
        id=args.repository_id,
        name=args.name,
        full_name=args.full_name,
        visibility=args.visibility,
        default_branch=args.default_branch,
        clone_url=args.clone_url or "",
        commit_sha=args.commit_sha,
    )
    orchestrator = AuditOrchestrator(
        policy=policy,
        runtime_root=args.runtime,
        github_client=_client(policy),
    )
    report = orchestrator.audit_repository(descriptor, args.path)
    print(json.dumps(report.to_dict(), indent=2))


def command_audit_all(args: argparse.Namespace) -> None:
    policy = _policy(args)
    inventory, repositories = load_inventory(args.inventory)
    owner = str(inventory["owner"])
    if args.live_inventory:
        live_repositories = _client(policy).list_repositories(owner)
        repositories = merge_inventory(repositories, live_repositories)
        inventory = inventory_payload(
            owner,
            repositories,
            source="github_api_merged_with_authenticated_snapshot",
        )
    orchestrator = AuditOrchestrator(
        policy=policy,
        runtime_root=args.runtime,
        github_client=_client(policy),
    )
    run_root, portfolio = orchestrator.audit_all(
        owner=owner,
        repositories=repositories,
        inventory=inventory,
        local_roots=args.source_root,
        download_missing=args.download_missing,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "run_root": str(run_root),
                "repository_count": portfolio["repository_count"],
                "status_counts": portfolio["status_counts"],
                "finding_counts": portfolio["finding_counts"],
            },
            indent=2,
        )
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Read-only Agentic Engineering repository audit control plane"
    )
    root.add_argument("--policy", default=str(DEFAULT_POLICY))
    root.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    commands = root.add_subparsers(dest="command", required=True)

    agents = commands.add_parser("agents")
    agents.set_defaults(func=command_agents)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    doctor.set_defaults(func=command_doctor)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("--owner", default="SuriyaBoon")
    inventory.add_argument("--output")
    inventory.set_defaults(func=command_inventory)

    audit_repo = commands.add_parser("audit-repo")
    audit_repo.add_argument("--path", required=True)
    audit_repo.add_argument("--name", required=True)
    audit_repo.add_argument("--full-name", required=True)
    audit_repo.add_argument("--repository-id", type=int, default=0)
    audit_repo.add_argument("--visibility", default="public")
    audit_repo.add_argument("--default-branch", default="main")
    audit_repo.add_argument("--clone-url")
    audit_repo.add_argument("--commit-sha")
    audit_repo.set_defaults(func=command_audit_repo)

    audit_all = commands.add_parser("audit-all")
    audit_all.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    audit_all.add_argument("--source-root", action="append", default=[])
    audit_all.add_argument("--download-missing", action="store_true")
    audit_all.add_argument("--live-inventory", action="store_true")
    audit_all.set_defaults(func=command_audit_all)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
