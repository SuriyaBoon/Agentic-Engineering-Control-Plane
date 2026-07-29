from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .agents import default_agents
from .development import (
    DevelopmentController,
    DevelopmentPolicy,
    RepositoryOnboardingController,
    RepositoryRegistry,
)
from .github_client import GitHubClient
from .inventory import inventory_payload, load_inventory, merge_inventory, write_inventory
from .models import RepositoryDescriptor
from .orchestrator import AuditOrchestrator
from .policy import AuditPolicy
from .workflow import GovernedWorkflow


REPOSITORY_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPOSITORY_ROOT / "config" / "policy.json"
DEFAULT_INVENTORY = REPOSITORY_ROOT / "config" / "inventory-snapshot.json"
DEFAULT_DEVELOPMENT_POLICY = REPOSITORY_ROOT / "config" / "development-policy.json"
DEFAULT_REPOSITORIES = REPOSITORY_ROOT / "config" / "repositories.json"
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


def _development(args: argparse.Namespace) -> DevelopmentController:
    overlay = Path(args.runtime) / "development" / "active-repositories.json"
    return DevelopmentController(
        policy=DevelopmentPolicy.load(args.development_policy),
        registry=RepositoryRegistry(args.repositories, overlay),
        runtime_root=args.runtime,
    )


def _onboarding(args: argparse.Namespace) -> RepositoryOnboardingController:
    overlay = Path(args.runtime) / "development" / "active-repositories.json"
    return RepositoryOnboardingController(
        policy=DevelopmentPolicy.load(args.development_policy),
        registry=RepositoryRegistry(args.repositories, overlay),
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
    payload["development_agents"] = [
        "intent_guard",
        "planner",
        "workspace_manager",
        "coder",
        "test_runner",
        "security_reviewer",
        "independent_reviewer",
        "human_publish_approver",
        "publisher",
        "post_merge_verifier",
        "memory_monitor",
    ]
    payload["development_tools"] = {
        "isolated_clone": True,
        "bounded_change_set": True,
        "registered_test_runner": True,
        "draft_pr_after_human_approval": True,
        "direct_default_branch_push": False,
        "automatic_merge": False,
        "production_action": False,
    }
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
        "development_protocol_ready": True,
        "dynamic_repository_onboarding_ready": True,
        "registered_development_repositories": len(
            RepositoryRegistry(
                args.repositories,
                Path(args.runtime) / "development" / "active-repositories.json",
            ).list()
        ),
        "draft_pr_after_human_approval": True,
        "automatic_merge_enabled": False,
        "production_actions_enabled": False,
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


def command_repo_list(args: argparse.Namespace) -> None:
    _print(_development(args).registry.list(include_inactive=args.all))


def command_repo_discover(args: argparse.Namespace) -> None:
    repositories = [
        item.to_dict()
        for item in _client(_policy(args)).list_repositories(args.owner)
    ]
    _print(_onboarding(args).discover(repositories, actor=args.actor))


def command_repo_assess(args: argparse.Namespace) -> None:
    _print(_onboarding(args).assess(args.full_name, actor=args.actor))


def command_repo_contract_set(args: argparse.Namespace) -> None:
    commands = [json.loads(value) for value in args.command_json]
    _print(
        _onboarding(args).set_contract(
            args.full_name,
            commands=commands,
            actor=args.actor,
        )
    )


def command_repo_onboard_approve(args: argparse.Namespace) -> None:
    _print(
        _onboarding(args).approve(
            args.full_name,
            actor=args.actor,
            confirmation=args.confirm,
            comment=args.comment,
        )
    )


def command_repo_activate(args: argparse.Namespace) -> None:
    _print(_onboarding(args).activate(args.full_name, actor=args.actor))


def command_repo_suspend(args: argparse.Namespace) -> None:
    _print(
        _onboarding(args).suspend(
            args.repository,
            actor=args.actor,
            confirmation=args.confirm,
            reason=args.reason,
        )
    )


def command_repo_resume_onboarding(args: argparse.Namespace) -> None:
    _print(
        _onboarding(args).resume_onboarding(
            args.full_name,
            actor=args.actor,
            confirmation=args.confirm,
            reason=args.reason,
        )
    )


def command_repo_show(args: argparse.Namespace) -> None:
    _print(_onboarding(args).show(args.full_name))


def command_repo_onboarding_monitor(args: argparse.Namespace) -> None:
    _print(_onboarding(args).status())


def command_dev_start(args: argparse.Namespace) -> None:
    _print(
        _development(args).start(
            repository_name=args.repository,
            intent=args.intent,
            acceptance_criteria=args.acceptance,
            actor=args.actor,
        )
    )


def command_dev_plan(args: argparse.Namespace) -> None:
    _print(_development(args).plan(args.task_id, actor=args.actor))


def command_dev_cancel(args: argparse.Namespace) -> None:
    _print(
        _development(args).cancel(
            args.task_id,
            actor=args.actor,
            reason=args.reason,
        )
    )


def command_dev_prepare(args: argparse.Namespace) -> None:
    _print(_development(args).prepare(args.task_id, actor=args.actor))


def command_dev_apply(args: argparse.Namespace) -> None:
    _print(
        _development(args).apply_changes(
            args.task_id,
            change_set_path=args.change_set,
            actor=args.actor,
        )
    )


def command_dev_test(args: argparse.Namespace) -> None:
    _print(_development(args).test(args.task_id, actor=args.actor))


def command_dev_review(args: argparse.Namespace) -> None:
    _print(
        _development(args).review(
            args.task_id,
            actor=args.actor,
            acceptance_evidence=args.acceptance_evidence,
        )
    )


def command_dev_approve(args: argparse.Namespace) -> None:
    _print(
        _development(args).approve_publish(
            args.task_id,
            actor=args.actor,
            confirmation=args.confirm,
            comment=args.comment,
        )
    )


def command_dev_evidence(args: argparse.Namespace) -> None:
    _print(_development(args).build_evidence(args.task_id, actor=args.actor))


def command_dev_publish(args: argparse.Namespace) -> None:
    _print(
        _development(args).publish(
            args.task_id,
            actor=args.actor,
            title=args.title,
            body=args.body,
        )
    )


def command_dev_verify_merge(args: argparse.Namespace) -> None:
    _print(_development(args).verify_merge(args.task_id, actor=args.actor))


def command_dev_show(args: argparse.Namespace) -> None:
    controller = _development(args)
    task = controller.store.load(args.task_id)
    task["events"] = controller.store.events(args.task_id)
    task["event_chain_valid"] = controller.store.verify_chain(args.task_id)
    _print(task)


def command_dev_monitor(args: argparse.Namespace) -> None:
    _print(_development(args).monitor())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Governed Agentic Engineering audit and remediation control plane"
    )
    root.add_argument("--policy", default=str(DEFAULT_POLICY))
    root.add_argument(
        "--development-policy",
        default=str(DEFAULT_DEVELOPMENT_POLICY),
    )
    root.add_argument("--repositories", default=str(DEFAULT_REPOSITORIES))
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

    repo_list = commands.add_parser("repo-list")
    repo_list.add_argument("--all", action="store_true")
    repo_list.set_defaults(func=command_repo_list)

    for name in ("repo-discover", "repo-sync"):
        repo_discover = commands.add_parser(name)
        repo_discover.add_argument("--owner", default="SuriyaBoon")
        repo_discover.add_argument("--actor", required=True)
        repo_discover.set_defaults(func=command_repo_discover)

    repo_assess = commands.add_parser("repo-assess")
    repo_assess.add_argument("--full-name", required=True)
    repo_assess.add_argument("--actor", required=True)
    repo_assess.set_defaults(func=command_repo_assess)

    repo_contract = commands.add_parser("repo-contract-set")
    repo_contract.add_argument("--full-name", required=True)
    repo_contract.add_argument(
        "--command-json",
        action="append",
        required=True,
        help='JSON argument array, for example ["python","-m","unittest"]',
    )
    repo_contract.add_argument("--actor", required=True)
    repo_contract.set_defaults(func=command_repo_contract_set)

    repo_approve = commands.add_parser("repo-onboard-approve")
    repo_approve.add_argument("--full-name", required=True)
    repo_approve.add_argument("--actor", required=True)
    repo_approve.add_argument("--confirm", required=True)
    repo_approve.add_argument("--comment", required=True)
    repo_approve.set_defaults(func=command_repo_onboard_approve)

    repo_activate = commands.add_parser("repo-activate")
    repo_activate.add_argument("--full-name", required=True)
    repo_activate.add_argument("--actor", required=True)
    repo_activate.set_defaults(func=command_repo_activate)

    repo_suspend = commands.add_parser("repo-suspend")
    repo_suspend.add_argument("--repository", required=True)
    repo_suspend.add_argument("--actor", required=True)
    repo_suspend.add_argument("--confirm", required=True)
    repo_suspend.add_argument("--reason", required=True)
    repo_suspend.set_defaults(func=command_repo_suspend)

    repo_resume = commands.add_parser("repo-resume-onboarding")
    repo_resume.add_argument("--full-name", required=True)
    repo_resume.add_argument("--actor", required=True)
    repo_resume.add_argument("--confirm", required=True)
    repo_resume.add_argument("--reason", required=True)
    repo_resume.set_defaults(func=command_repo_resume_onboarding)

    repo_show = commands.add_parser("repo-show")
    repo_show.add_argument("--full-name", required=True)
    repo_show.set_defaults(func=command_repo_show)

    repo_onboarding_monitor = commands.add_parser("repo-onboarding-monitor")
    repo_onboarding_monitor.set_defaults(func=command_repo_onboarding_monitor)

    dev_start = commands.add_parser("dev-start")
    dev_start.add_argument("--repository", required=True)
    dev_start.add_argument("--intent", required=True)
    dev_start.add_argument("--acceptance", action="append", required=True)
    dev_start.add_argument("--actor", required=True)
    dev_start.set_defaults(func=command_dev_start)

    dev_plan = commands.add_parser("dev-plan")
    dev_plan.add_argument("--task-id", required=True)
    dev_plan.add_argument("--actor", required=True)
    dev_plan.set_defaults(func=command_dev_plan)

    dev_cancel = commands.add_parser("dev-cancel")
    dev_cancel.add_argument("--task-id", required=True)
    dev_cancel.add_argument("--actor", required=True)
    dev_cancel.add_argument("--reason", required=True)
    dev_cancel.set_defaults(func=command_dev_cancel)

    dev_prepare = commands.add_parser("dev-prepare")
    dev_prepare.add_argument("--task-id", required=True)
    dev_prepare.add_argument("--actor", required=True)
    dev_prepare.set_defaults(func=command_dev_prepare)

    dev_apply = commands.add_parser("dev-apply")
    dev_apply.add_argument("--task-id", required=True)
    dev_apply.add_argument("--change-set", required=True)
    dev_apply.add_argument("--actor", required=True)
    dev_apply.set_defaults(func=command_dev_apply)

    dev_test = commands.add_parser("dev-test")
    dev_test.add_argument("--task-id", required=True)
    dev_test.add_argument("--actor", required=True)
    dev_test.set_defaults(func=command_dev_test)

    dev_review = commands.add_parser("dev-review")
    dev_review.add_argument("--task-id", required=True)
    dev_review.add_argument("--actor", required=True)
    dev_review.add_argument(
        "--acceptance-evidence",
        action="append",
        required=True,
    )
    dev_review.set_defaults(func=command_dev_review)

    dev_approve = commands.add_parser("dev-approve")
    dev_approve.add_argument("--task-id", required=True)
    dev_approve.add_argument("--actor", required=True)
    dev_approve.add_argument("--confirm", required=True)
    dev_approve.add_argument("--comment", required=True)
    dev_approve.set_defaults(func=command_dev_approve)

    dev_evidence = commands.add_parser("dev-evidence")
    dev_evidence.add_argument("--task-id", required=True)
    dev_evidence.add_argument("--actor", required=True)
    dev_evidence.set_defaults(func=command_dev_evidence)

    dev_publish = commands.add_parser("dev-publish")
    dev_publish.add_argument("--task-id", required=True)
    dev_publish.add_argument("--actor", required=True)
    dev_publish.add_argument("--title", required=True)
    dev_publish.add_argument("--body", required=True)
    dev_publish.set_defaults(func=command_dev_publish)

    dev_verify = commands.add_parser("dev-verify-merge")
    dev_verify.add_argument("--task-id", required=True)
    dev_verify.add_argument("--actor", required=True)
    dev_verify.set_defaults(func=command_dev_verify_merge)

    dev_show = commands.add_parser("dev-show")
    dev_show.add_argument("--task-id", required=True)
    dev_show.set_defaults(func=command_dev_show)

    dev_monitor = commands.add_parser("dev-monitor")
    dev_monitor.set_defaults(func=command_dev_monitor)

    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
