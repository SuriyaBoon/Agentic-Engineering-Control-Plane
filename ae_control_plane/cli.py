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
from .workflow import GovernedWorkflow


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


def _workflow(args: argparse.Namespace) -> GovernedWorkflow:
    return GovernedWorkflow(
        policy=_policy(args),
        runtime_root=args.runtime,
    )


def _print(payload: dict | list) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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
    payload["lifecycle_agents"] = [
        "intent_guard",
        "planner",
        "tool_router",
        "human_execution_approver",
        "safe_executor",
        "validator",
        "human_closure_approver",
        "memory",
        "monitor",
        "response_generator",
    ]
    payload["tools"] = [
        item.to_dict() for item in _workflow(args).registry.list()
    ]
    _print(payload)


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
        "workflow_database": str(
            (Path(args.runtime) / "workflow" / "workflow.db").resolve()
        ),
        "governed_lifecycle_ready": True,
        "live_remediation_enabled": False,
    }
    if private and not payload["github_token_present"]:
        payload["status"] = "ready_public_auth_required_private"
    _print(payload)


def command_inventory(args: argparse.Namespace) -> None:
    policy = _policy(args)
    repositories = _client(policy).list_repositories(args.owner)
    payload = inventory_payload(args.owner, repositories, source="github_api")
    if args.output:
        write_inventory(args.output, payload)
    _print(payload)


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
    _print(report.to_dict())


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
    result = {
        "status": "completed",
        "run_root": str(run_root),
        "repository_count": portfolio["repository_count"],
        "status_counts": portfolio["status_counts"],
        "finding_counts": portfolio["finding_counts"],
    }
    if args.create_work_items:
        result["workflow"] = GovernedWorkflow(
            policy=policy,
            runtime_root=args.runtime,
        ).ingest_run(run_root)
    _print(result)


def command_workflow_ingest(args: argparse.Namespace) -> None:
    _print(_workflow(args).ingest_run(args.run_root, actor=args.actor))


def command_work_list(args: argparse.Namespace) -> None:
    items = _workflow(args).store.list_work_items(args.state)
    _print([item.to_dict() for item in items])


def command_work_show(args: argparse.Namespace) -> None:
    workflow = _workflow(args)
    item = workflow.store.get_work_item(args.work_item_id)
    payload = {
        "work_item": item.to_dict(),
        "events": workflow.store.events(args.work_item_id),
        "approvals": [
            approval.to_dict()
            for approval in workflow.store.approvals(args.work_item_id)
        ],
    }
    try:
        payload["plan"] = workflow.store.get_plan(args.work_item_id).to_dict()
    except KeyError:
        payload["plan"] = None
    try:
        payload["latest_execution"] = workflow.store.latest_execution(
            args.work_item_id
        ).to_dict()
    except KeyError:
        payload["latest_execution"] = None
    _print(payload)


def command_work_triage(args: argparse.Namespace) -> None:
    _print(
        _workflow(args).triage(
            args.work_item_id,
            actor=args.actor,
            owner=args.owner,
        ).to_dict()
    )


def command_work_plan(args: argparse.Namespace) -> None:
    _print(
        _workflow(args).plan(
            args.work_item_id,
            actor=args.actor,
        ).to_dict()
    )


def command_work_approve(args: argparse.Namespace) -> None:
    workflow = _workflow(args)
    if args.stage == "execution":
        item = workflow.approve_execution(
            args.work_item_id,
            actor=args.actor,
            decision=args.decision,
            comment=args.comment,
        )
    else:
        item = workflow.approve_closure(
            args.work_item_id,
            actor=args.actor,
            decision=args.decision,
            comment=args.comment,
        )
    _print(item.to_dict())


def command_work_execute(args: argparse.Namespace) -> None:
    _print(
        _workflow(args).execute(
            args.work_item_id,
            actor=args.actor,
        ).to_dict()
    )


def command_work_validate(args: argparse.Namespace) -> None:
    _print(
        _workflow(args).validate(
            args.work_item_id,
            actor=args.actor,
        ).to_dict()
    )


def command_monitor(args: argparse.Namespace) -> None:
    _print(_workflow(args).status())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Governed Agentic Engineering audit and remediation control plane"
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
    audit_all.add_argument("--create-work-items", action="store_true")
    audit_all.set_defaults(func=command_audit_all)

    workflow_ingest = commands.add_parser("workflow-ingest")
    workflow_ingest.add_argument("--run-root", required=True)
    workflow_ingest.add_argument("--actor", default="audit-ingest")
    workflow_ingest.set_defaults(func=command_workflow_ingest)

    work_list = commands.add_parser("work-list")
    work_list.add_argument("--state")
    work_list.set_defaults(func=command_work_list)

    work_show = commands.add_parser("work-show")
    work_show.add_argument("--work-item-id", required=True)
    work_show.set_defaults(func=command_work_show)

    work_triage = commands.add_parser("work-triage")
    work_triage.add_argument("--work-item-id", required=True)
    work_triage.add_argument("--actor", required=True)
    work_triage.add_argument("--owner", required=True)
    work_triage.set_defaults(func=command_work_triage)

    work_plan = commands.add_parser("work-plan")
    work_plan.add_argument("--work-item-id", required=True)
    work_plan.add_argument("--actor", required=True)
    work_plan.set_defaults(func=command_work_plan)

    work_approve = commands.add_parser("work-approve")
    work_approve.add_argument("--work-item-id", required=True)
    work_approve.add_argument(
        "--stage",
        required=True,
        choices=("execution", "closure"),
    )
    work_approve.add_argument(
        "--decision",
        required=True,
        choices=("approved", "rejected"),
    )
    work_approve.add_argument("--actor", required=True)
    work_approve.add_argument("--comment", required=True)
    work_approve.set_defaults(func=command_work_approve)

    work_execute = commands.add_parser("work-execute")
    work_execute.add_argument("--work-item-id", required=True)
    work_execute.add_argument("--actor", required=True)
    work_execute.set_defaults(func=command_work_execute)

    work_validate = commands.add_parser("work-validate")
    work_validate.add_argument("--work-item-id", required=True)
    work_validate.add_argument("--actor", required=True)
    work_validate.set_defaults(func=command_work_validate)

    monitor = commands.add_parser("monitor")
    monitor.set_defaults(func=command_monitor)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
