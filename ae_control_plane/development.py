from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeVar

from .isolation import DockerTestRunner, IsolationConfig
from .locking import FileMutex


T = TypeVar("T")


TASK_STATES = {
    "created",
    "planned",
    "workspace_ready",
    "changes_applied",
    "tests_passed",
    "tests_failed",
    "review_passed",
    "review_failed",
    "awaiting_publish_approval",
    "publish_approved",
    "published",
    "merged_verified",
    "manual_review",
    "cancelled",
}

TRANSITIONS = {
    "created": {"planned", "manual_review", "cancelled"},
    "planned": {"workspace_ready", "manual_review", "cancelled"},
    "workspace_ready": {"changes_applied", "manual_review", "cancelled"},
    "changes_applied": {"tests_passed", "tests_failed", "manual_review", "cancelled"},
    "tests_failed": {"changes_applied", "manual_review", "cancelled"},
    "tests_passed": {"review_passed", "review_failed", "manual_review", "cancelled"},
    "review_failed": {"changes_applied", "manual_review", "cancelled"},
    "review_passed": {"awaiting_publish_approval", "manual_review", "cancelled"},
    "awaiting_publish_approval": {"publish_approved", "manual_review", "cancelled"},
    "publish_approved": {"published", "manual_review"},
    "published": {"merged_verified", "manual_review"},
    "merged_verified": set(),
    "manual_review": {"planned", "changes_applied", "cancelled"},
    "cancelled": set(),
}

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if argv and Path(argv[0]).name.lower() in {"git", "git.exe"}:
        if (cwd / ".git").exists():
            argv = [argv[0], "-c", f"safe.directory={cwd.resolve()}", *argv[1:]]
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"command failed to start: {argv[0]}: {exc}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"command failed ({result.returncode}): {argv[0]}: {detail}")
    return result


@dataclass(frozen=True)
class DevelopmentPolicy:
    allowed_owners: tuple[str, ...]
    workspace_root: str
    max_changed_files: int
    max_added_lines: int
    max_deleted_lines: int
    max_file_bytes: int
    max_repair_attempts: int
    test_timeout_seconds: int
    allowed_test_executables: tuple[str, ...]
    require_independent_review: bool
    require_human_publish_approval: bool
    draft_pr_only: bool
    direct_default_branch_push: bool
    production_actions_enabled: bool
    required_post_merge_checks: tuple[str, ...]
    test_isolation: IsolationConfig

    @classmethod
    def load(cls, path: str | Path) -> "DevelopmentPolicy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("mode") != "governed_development":
            raise ValueError("development policy must use governed_development mode")
        guardrails = payload["guardrails"]
        if guardrails.get("direct_default_branch_push") is not False:
            raise ValueError("direct default-branch push must remain disabled")
        if guardrails.get("production_actions_enabled") is not False:
            raise ValueError("production actions must remain disabled")
        if guardrails.get("draft_pr_only") is not True:
            raise ValueError("pull requests must be draft-only")
        required_checks = guardrails.get("required_post_merge_checks")
        if (
            not isinstance(required_checks, list)
            or not required_checks
            or any(
                not isinstance(name, str) or not name.strip()
                for name in required_checks
            )
            or len(set(required_checks)) != len(required_checks)
        ):
            raise ValueError(
                "required_post_merge_checks must contain unique check names"
            )
        limits = payload["limits"]
        return cls(
            allowed_owners=tuple(payload["allowed_owners"]),
            workspace_root=str(payload["workspace_root"]),
            max_changed_files=int(limits["max_changed_files"]),
            max_added_lines=int(limits["max_added_lines"]),
            max_deleted_lines=int(limits["max_deleted_lines"]),
            max_file_bytes=int(limits["max_file_bytes"]),
            max_repair_attempts=int(limits["max_repair_attempts"]),
            test_timeout_seconds=int(limits["test_timeout_seconds"]),
            allowed_test_executables=tuple(payload["allowed_test_executables"]),
            require_independent_review=bool(
                guardrails["require_independent_review"]
            ),
            require_human_publish_approval=bool(
                guardrails["require_human_publish_approval"]
            ),
            draft_pr_only=True,
            direct_default_branch_push=False,
            production_actions_enabled=False,
            required_post_merge_checks=tuple(required_checks),
            test_isolation=IsolationConfig.from_dict(
                payload.get("test_execution")
            ),
        )


def serialized_mutation(method: Callable[..., T]) -> Callable[..., T]:
    @wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
        with self.store.mutation_lock():
            return method(self, *args, **kwargs)

    return wrapper


def execute_test_command(
    policy: DevelopmentPolicy,
    argv: list[str],
    *,
    workspace: Path,
    output_dir: Path,
) -> subprocess.CompletedProcess[str]:
    if not isinstance(argv, list) or not argv:
        raise ValueError("test commands must be non-empty argument arrays")
    executable = Path(str(argv[0])).name.lower()
    if executable not in policy.allowed_test_executables:
        raise PermissionError(f"test executable is not allowed: {executable}")
    if policy.test_isolation.mode == "docker":
        return DockerTestRunner(policy.test_isolation).run(
            [str(part) for part in argv],
            workspace=workspace,
            output_dir=output_dir,
            timeout=policy.test_timeout_seconds,
        )
    resolved = [str(part) for part in argv]
    if executable in {"python", "python.exe"}:
        resolved[0] = sys.executable
    return run_command(
        resolved,
        cwd=workspace,
        timeout=policy.test_timeout_seconds,
        check=False,
    )


class RepositoryRegistry:
    def __init__(
        self,
        path: str | Path,
        overlay_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.repositories = {
            item["name"].lower(): {**item, "status": item.get("status", "active")}
            for item in payload["repositories"]
        }
        self.overlay_path = Path(overlay_path) if overlay_path else None
        if self.overlay_path and self.overlay_path.exists():
            overlay = json.loads(self.overlay_path.read_text(encoding="utf-8"))
            for item in overlay.get("repositories", []):
                self.repositories[item["name"].lower()] = dict(item)

    def get(self, name: str) -> dict[str, Any]:
        try:
            repository = dict(self.repositories[name.lower()])
        except KeyError as exc:
            raise KeyError(f"repository is not registered: {name}") from exc
        if repository.get("status", "active") != "active":
            raise PermissionError(
                f"repository is not active: {name}: {repository.get('status')}"
            )
        return repository

    def list(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        results = []
        for key in sorted(self.repositories):
            item = dict(self.repositories[key])
            if include_inactive or item.get("status", "active") == "active":
                results.append(item)
        return results

    def save_overlay(self, repository: dict[str, Any]) -> None:
        if self.overlay_path is None:
            raise RuntimeError("repository registry overlay is not configured")
        self.repositories[repository["name"].lower()] = dict(repository)
        base_names = {
            item["name"].lower()
            for item in json.loads(self.path.read_text(encoding="utf-8"))[
                "repositories"
            ]
        }
        overlay_items = [
            dict(item)
            for key, item in sorted(self.repositories.items())
            if key not in base_names or item.get("status") != "active"
        ]
        self.overlay_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.overlay_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "repositories": overlay_items,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.overlay_path)


class DevelopmentStore:
    def __init__(self, runtime_root: str | Path) -> None:
        runtime = Path(runtime_root)
        self.root = runtime / "development"
        self.lock_path = runtime / ".lifecycle-mutation.lock"
        self.tasks_root = self.root / "tasks"
        self.tasks_root.mkdir(parents=True, exist_ok=True)

    def mutation_lock(self) -> FileMutex:
        return FileMutex(self.lock_path, timeout_seconds=660)

    def task_root(self, task_id: str) -> Path:
        if not re.fullmatch(r"DEV-[a-f0-9]{16}", task_id):
            raise ValueError("invalid task id")
        return self.tasks_root / task_id

    def create(self, task: dict[str, Any], actor: str) -> dict[str, Any]:
        root = self.task_root(task["task_id"])
        if root.exists():
            return self.load(task["task_id"])
        root.mkdir(parents=True)
        self._write_state(root, task)
        self.append_event(
            task["task_id"], "task_created", actor, {"state": "created"}
        )
        return task

    def load(self, task_id: str) -> dict[str, Any]:
        return json.loads(
            (self.task_root(task_id) / "state.json").read_text(encoding="utf-8")
        )

    def save(self, task: dict[str, Any]) -> None:
        target = self.task_root(task["task_id"]) / "state.json"
        if target.is_file():
            current = json.loads(target.read_text(encoding="utf-8"))
            if current.get("updated_at") != task.get("updated_at"):
                raise RuntimeError("stale development task mutation rejected")
        task["updated_at"] = utc_now()
        self._write_state(self.task_root(task["task_id"]), task)

    @staticmethod
    def _write_state(root: Path, task: dict[str, Any]) -> None:
        target = root / "state.json"
        temporary = root / "state.json.tmp"
        temporary.write_text(
            json.dumps(task, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def append_event(
        self,
        task_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        path = self.task_root(task_id) / "events.jsonl"
        previous_hash = "GENESIS"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            if lines:
                previous_hash = json.loads(lines[-1])["event_hash"]
        event = {
            "task_id": task_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": utc_now(),
        }
        event["event_hash"] = hashlib.sha256(
            canonical_json(event).encode("utf-8")
        ).hexdigest()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(event) + "\n")

    def events(self, task_id: str) -> list[dict[str, Any]]:
        path = self.task_root(task_id) / "events.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def verify_chain(self, task_id: str) -> bool:
        return self.verify_events(self.events(task_id))

    @staticmethod
    def verify_events(events: list[dict[str, Any]]) -> bool:
        previous_hash = "GENESIS"
        for source_event in events:
            event = dict(source_event)
            actual = event.pop("event_hash", None)
            expected = hashlib.sha256(
                canonical_json(event).encode("utf-8")
            ).hexdigest()
            if event["previous_hash"] != previous_hash or actual != expected:
                return False
            previous_hash = actual
        return True

    def transition(
        self,
        task: dict[str, Any],
        target: str,
        *,
        actor: str,
        reason: str,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = task["state"]
        if target not in TRANSITIONS[current]:
            raise ValueError(f"invalid development transition: {current} -> {target}")
        task.update(updates or {})
        task["state"] = target
        self.save(task)
        self.append_event(
            task["task_id"],
            "state_transition",
            actor,
            {"from": current, "to": target, "reason": reason},
        )
        return task

    def list_tasks(self) -> list[dict[str, Any]]:
        results = []
        for path in sorted(self.tasks_root.glob("DEV-*/state.json")):
            results.append(json.loads(path.read_text(encoding="utf-8")))
        return results


class DevelopmentController:
    def __init__(
        self,
        *,
        policy: DevelopmentPolicy,
        registry: RepositoryRegistry,
        runtime_root: str | Path,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.store = DevelopmentStore(runtime_root)

    def _resolve_local_path(self, repository: dict[str, Any]) -> Path | None:
        value = repository.get("local_path")
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = (self.registry.path.parent / path).resolve()
        return path

    @staticmethod
    def _source(repository: dict[str, Any]) -> str:
        source = str(repository.get("clone_url", "")).strip()
        if not source:
            raise ValueError("repository clone_url is required")
        return source

    def _check_owner(self, repository: dict[str, Any]) -> None:
        owner = str(repository["full_name"]).split("/", 1)[0]
        if owner not in self.policy.allowed_owners:
            raise PermissionError(f"repository owner is not allowed: {owner}")

    def _repository_required_checks(
        self, repository: dict[str, Any]
    ) -> tuple[str, ...]:
        raw = repository.get(
            "required_post_merge_checks",
            self.policy.required_post_merge_checks,
        )
        if (
            not isinstance(raw, (list, tuple))
            or not raw
            or any(not isinstance(name, str) or not name.strip() for name in raw)
        ):
            raise ValueError(
                "required_post_merge_checks must be a non-empty list of check names"
            )
        normalized = tuple(name.strip() for name in raw)
        if len(set(normalized)) != len(normalized):
            raise ValueError("required_post_merge_checks must contain unique check names")
        return normalized

    def _task_required_checks(self, task: dict[str, Any]) -> tuple[str, ...]:
        snapshot = task.get("required_post_merge_checks")
        if snapshot is not None:
            return self._repository_required_checks(
                {"required_post_merge_checks": snapshot}
            )
        # Backward-compatible path for tasks created before check contracts were
        # snapshotted. The active registry must explicitly carry the contract;
        # otherwise the global fail-closed policy remains authoritative.
        repository = self.registry.get(task["repository"]["name"])
        return self._repository_required_checks(repository)

    @serialized_mutation
    def start(
        self,
        *,
        repository_name: str,
        intent: str,
        acceptance_criteria: list[str],
        actor: str,
    ) -> dict[str, Any]:
        if not intent.strip() or not acceptance_criteria:
            raise ValueError("intent and acceptance criteria are required")
        repository = self.registry.get(repository_name)
        self._check_owner(repository)
        required_checks = self._repository_required_checks(repository)
        risk = self._classify_risk(intent)
        base = str(repository["default_branch"])
        task_id = stable_id(
            "DEV", repository["full_name"], intent, canonical_json(acceptance_criteria)
        )
        task = {
            "schema_version": "1.0.0",
            "task_id": task_id,
            "repository": repository,
            "intent": intent.strip(),
            "acceptance_criteria": acceptance_criteria,
            "risk": risk,
            "state": "created",
            "base_branch": base,
            "branch": f"agentic/{task_id.lower()}",
            "required_post_merge_checks": list(required_checks),
            "source_sha": None,
            "workspace": None,
            "plan": None,
            "coder": None,
            "reviewer": None,
            "publish_approval": None,
            "test_results": [],
            "change_metrics": {},
            "repair_attempts": 0,
            "evidence_manifest": None,
            "pull_request": None,
            "created_by": actor,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        return self.store.create(task, actor)

    @staticmethod
    def _classify_risk(intent: str) -> str:
        lowered = intent.lower()
        high = ("auth", "secret", "permission", "database", "migration", "security")
        critical = ("production", "deploy", "credential rotation", "delete repository")
        if any(term in lowered for term in critical):
            return "critical"
        if any(term in lowered for term in high):
            return "high"
        return "medium"

    @serialized_mutation
    def plan(self, task_id: str, *, actor: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        plan = {
            "planner": actor,
            "steps": [
                "Prepare an immutable source identity and isolated clone.",
                "Route a bounded change set to the coder agent.",
                "Run the repository test contract.",
                "Require independent diff, secret, budget, and evidence review.",
                "Stop for explicit human publish approval.",
                "Commit, push only the task branch, and create a draft PR.",
                "Verify merge and post-merge checks before closure.",
            ],
            "agents": [
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
            ],
            "fallback": "manual_review",
            "max_repair_attempts": self.policy.max_repair_attempts,
        }
        return self.store.transition(
            task,
            "planned",
            actor=actor,
            reason="Intent classified and bounded plan created.",
            updates={"plan": plan},
        )

    @serialized_mutation
    def cancel(self, task_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        if not actor.strip() or not reason.strip():
            raise ValueError("cancellation actor and reason are required")
        return self.store.transition(
            task,
            "cancelled",
            actor=actor,
            reason=reason.strip(),
            updates={
                "cancelled_by": actor,
                "cancelled_at": utc_now(),
                "cancellation_reason": reason.strip(),
            },
        )

    @serialized_mutation
    def prepare(self, task_id: str, *, actor: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        repository = task["repository"]
        self._check_owner(repository)
        task_root = self.store.task_root(task_id)
        workspace = task_root / "workspace"
        if workspace.exists():
            if (workspace / ".git").exists():
                raise FileExistsError(f"isolated workspace already exists: {workspace}")
            if workspace.resolve().parent != task_root.resolve():
                raise PermissionError("invalid incomplete workspace path")
            shutil.rmtree(workspace)
        source = self._source(repository)
        run_command(
            [
                "git",
                "clone",
                "--no-tags",
                "--single-branch",
                "--branch",
                task["base_branch"],
                source,
                str(workspace),
            ],
            cwd=task_root,
            timeout=180,
        )
        sha = run_command(
            ["git", "rev-parse", "HEAD"], cwd=workspace
        ).stdout.strip()
        run_command(
            ["git", "checkout", "-b", task["branch"]],
            cwd=workspace,
        )
        return self.store.transition(
            task,
            "workspace_ready",
            actor=actor,
            reason="Immutable base captured and isolated task branch created.",
            updates={"workspace": str(workspace.resolve()), "source_sha": sha},
        )

    @staticmethod
    def _safe_relative(value: str) -> PurePosixPath:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise PermissionError(f"unsafe change path: {value}")
        if path.parts[0] == ".git":
            raise PermissionError("changes to .git are prohibited")
        return path

    @serialized_mutation
    def apply_changes(
        self,
        task_id: str,
        *,
        change_set_path: str | Path,
        actor: str,
    ) -> dict[str, Any]:
        task = self.store.load(task_id)
        if task["state"] not in {"workspace_ready", "tests_failed", "review_failed", "manual_review"}:
            raise ValueError(f"cannot apply changes from state: {task['state']}")
        if task["coder"] and task["coder"] != actor:
            raise PermissionError("repair attempts must retain the assigned coder")
        if task["repair_attempts"] >= self.policy.max_repair_attempts:
            return self.store.transition(
                task,
                "manual_review",
                actor=actor,
                reason="Repair budget exhausted.",
            )
        payload = json.loads(Path(change_set_path).read_text(encoding="utf-8"))
        operations = payload.get("operations", [])
        if not operations or len(operations) > self.policy.max_changed_files:
            raise ValueError("change set is empty or exceeds file budget")
        workspace = Path(task["workspace"])
        for operation in operations:
            relative = self._safe_relative(str(operation["path"]))
            target = workspace.joinpath(*relative.parts)
            resolved = target.resolve()
            if workspace.resolve() not in resolved.parents:
                raise PermissionError(f"change escapes workspace: {relative}")
            op = operation["op"]
            if op == "write":
                content = str(operation.get("content", ""))
                encoded = content.encode("utf-8")
                if len(encoded) > self.policy.max_file_bytes:
                    raise ValueError(f"file exceeds size budget: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(encoded)
            elif op == "delete":
                if target.is_file():
                    target.unlink()
                else:
                    raise FileNotFoundError(f"delete target is not a file: {relative}")
            else:
                raise ValueError(f"unsupported change operation: {op}")
        metrics = self._diff_metrics(workspace)
        self._enforce_budget(metrics)
        digest = hashlib.sha256(
            Path(change_set_path).read_bytes()
        ).hexdigest()
        attempt = int(task["repair_attempts"]) + 1
        target = "changes_applied"
        if task["state"] == "manual_review":
            task["state"] = "review_failed"
        return self.store.transition(
            task,
            target,
            actor=actor,
            reason="Coder change set applied inside isolated workspace.",
            updates={
                "coder": actor,
                "repair_attempts": attempt,
                "change_metrics": metrics,
                "change_set_sha256": digest,
            },
        )

    @staticmethod
    def _diff_metrics(workspace: Path) -> dict[str, Any]:
        result = run_command(
            ["git", "diff", "--numstat", "--", "."],
            cwd=workspace,
            check=False,
        )
        files = []
        added = deleted = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            add = int(parts[0]) if parts[0].isdigit() else 0
            remove = int(parts[1]) if parts[1].isdigit() else 0
            added += add
            deleted += remove
            files.append(parts[2])
        untracked = run_command(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=workspace,
        ).stdout.splitlines()
        for relative in untracked:
            content = (workspace / relative).read_text(encoding="utf-8", errors="replace")
            added += len(content.splitlines())
            files.append(relative)
        return {
            "changed_files": sorted(set(files)),
            "changed_file_count": len(set(files)),
            "added_lines": added,
            "deleted_lines": deleted,
        }

    def _enforce_budget(self, metrics: dict[str, Any]) -> None:
        if metrics["changed_file_count"] > self.policy.max_changed_files:
            raise PermissionError("changed-file budget exceeded")
        if metrics["added_lines"] > self.policy.max_added_lines:
            raise PermissionError("added-line budget exceeded")
        if metrics["deleted_lines"] > self.policy.max_deleted_lines:
            raise PermissionError("deleted-line budget exceeded")

    @staticmethod
    def _change_digest(workspace: Path) -> str:
        digest = hashlib.sha256()
        diff = run_command(
            ["git", "diff", "--no-ext-diff", "--binary", "--", "."],
            cwd=workspace,
            check=False,
        ).stdout
        digest.update(diff.encode("utf-8"))
        for relative in sorted(
            run_command(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=workspace,
            ).stdout.splitlines()
        ):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update((workspace / relative).read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @serialized_mutation
    def test(self, task_id: str, *, actor: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        if task["state"] != "changes_applied":
            raise ValueError(f"cannot test from state: {task['state']}")
        if actor == task["coder"]:
            raise PermissionError("coder cannot act as the independent test runner")
        commands = task["repository"].get("test_commands", [])
        if not commands:
            return self.store.transition(
                task,
                "manual_review",
                actor=actor,
                reason="No test contract is registered for this repository.",
            )
        workspace = Path(task["workspace"])
        results = []
        passed = True
        for argv in commands:
            result = execute_test_command(
                self.policy,
                argv,
                workspace=workspace,
                output_dir=self.store.task_root(task_id) / "container-output",
            )
            results.append(
                {
                    "argv": argv,
                    "exit_code": result.returncode,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                }
            )
            if result.returncode:
                passed = False
                break
        target = "tests_passed" if passed else "tests_failed"
        return self.store.transition(
            task,
            target,
            actor=actor,
            reason="Repository test contract passed." if passed else "Test contract failed.",
            updates={"test_runner": actor, "test_results": results},
        )

    @serialized_mutation
    def review(
        self,
        task_id: str,
        *,
        actor: str,
        acceptance_evidence: list[str],
    ) -> dict[str, Any]:
        task = self.store.load(task_id)
        if task["state"] != "tests_passed":
            raise ValueError(f"cannot review from state: {task['state']}")
        if actor in {task.get("coder"), task.get("test_runner")}:
            raise PermissionError("reviewer must be independent from coder and test runner")
        workspace = Path(task["workspace"])
        metrics = self._diff_metrics(workspace)
        self._enforce_budget(metrics)
        diff = run_command(
            ["git", "diff", "--no-ext-diff", "--binary", "--", "."],
            cwd=workspace,
            check=False,
        ).stdout
        untracked_content = ""
        for relative in run_command(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=workspace,
        ).stdout.splitlines():
            untracked_content += (workspace / relative).read_text(
                encoding="utf-8", errors="replace"
            )
        failures = []
        if not metrics["changed_files"]:
            failures.append("no_changes")
        if any(pattern.search(diff + untracked_content) for pattern in SECRET_PATTERNS):
            failures.append("potential_secret_detected")
        if not task["test_results"] or any(
            result["exit_code"] != 0 for result in task["test_results"]
        ):
            failures.append("tests_not_verified")
        if len(acceptance_evidence) != len(task["acceptance_criteria"]):
            failures.append("acceptance_criteria_not_fully_verified")
        if any(not item.strip() for item in acceptance_evidence):
            failures.append("acceptance_evidence_empty")
        change_digest = self._change_digest(workspace)
        target = "review_failed" if failures else "review_passed"
        task = self.store.transition(
            task,
            target,
            actor=actor,
            reason="Independent review failed." if failures else "Independent review passed.",
            updates={
                "reviewer": actor,
                "review": {
                    "passed": not failures,
                    "failures": failures,
                    "acceptance_evidence": acceptance_evidence,
                    "change_sha256": change_digest,
                },
                "change_metrics": metrics,
            },
        )
        if not failures:
            task = self.store.transition(
                task,
                "awaiting_publish_approval",
                actor=actor,
                reason="All automated and independent gates passed.",
            )
        return task

    @serialized_mutation
    def approve_publish(
        self,
        task_id: str,
        *,
        actor: str,
        confirmation: str,
        comment: str,
    ) -> dict[str, Any]:
        task = self.store.load(task_id)
        if actor in {
            task.get("coder"),
            task.get("test_runner"),
            task.get("reviewer"),
        }:
            raise PermissionError("publish approver must be independent")
        expected = f"APPROVE {task_id}"
        if confirmation != expected:
            raise PermissionError(f"confirmation must exactly equal: {expected}")
        approval = {
            "actor": actor,
            "comment": comment,
            "confirmation": confirmation,
            "approved_at": utc_now(),
        }
        return self.store.transition(
            task,
            "publish_approved",
            actor=actor,
            reason="Human approved task-branch push and draft PR creation.",
            updates={"publish_approval": approval},
        )

    @serialized_mutation
    def build_evidence(self, task_id: str, *, actor: str) -> dict[str, Any]:
        return self._build_evidence(task_id, actor=actor)

    def _build_evidence(self, task_id: str, *, actor: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        root = self.store.task_root(task_id)
        if not self.store.verify_chain(task_id):
            raise RuntimeError("cannot package evidence from an invalid event chain")
        evidence_id = (
            datetime.now(timezone.utc).strftime("EVD-%Y%m%dT%H%M%S%fZ-")
            + os.urandom(4).hex()
        )
        package = root / "evidence" / evidence_id
        package.mkdir(parents=True)
        state_snapshot = package / "state.json"
        events_snapshot = package / "events.jsonl"
        state_snapshot.write_bytes((root / "state.json").read_bytes())
        events_snapshot.write_bytes((root / "events.jsonl").read_bytes())
        artifacts = {
            "state.json": sha256_file(state_snapshot),
            "events.jsonl": sha256_file(events_snapshot),
        }
        manifest = {
            "schema_version": "2.0.0",
            "evidence_id": evidence_id,
            "task_id": task_id,
            "repository": task["repository"]["full_name"],
            "source_sha": task["source_sha"],
            "branch": task["branch"],
            "snapshot_state": task["state"],
            "artifacts": artifacts,
            "event_chain_valid": self.store.verify_chain(task_id),
            "generated_at": utc_now(),
        }
        path = package / "manifest.json"
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.verify_evidence_manifest(path)
        task["evidence_manifest"] = {
            "evidence_id": evidence_id,
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
        }
        self.store.save(task)
        self.store.append_event(
            task_id, "evidence_manifest_created", actor, task["evidence_manifest"]
        )
        return task

    @staticmethod
    def verify_evidence_manifest(path: str | Path) -> dict[str, Any]:
        manifest_path = Path(path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "2.0.0":
            raise ValueError("unsupported evidence manifest schema")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "state.json",
            "events.jsonl",
        }:
            raise ValueError("evidence manifest artifact set is invalid")
        for name, expected in artifacts.items():
            artifact = manifest_path.parent / name
            if not artifact.is_file() or sha256_file(artifact) != expected:
                raise ValueError(f"evidence artifact hash mismatch: {name}")
        snapshot = json.loads(
            (manifest_path.parent / "state.json").read_text(encoding="utf-8")
        )
        if snapshot.get("task_id") != manifest.get("task_id"):
            raise ValueError("evidence task identity mismatch")
        events = [
            json.loads(line)
            for line in (manifest_path.parent / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        if not DevelopmentStore.verify_events(events):
            raise ValueError("evidence snapshot event chain is invalid")
        return manifest

    @serialized_mutation
    def publish(
        self,
        task_id: str,
        *,
        actor: str,
        title: str,
        body: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        task = self.store.load(task_id)
        if task["state"] != "publish_approved":
            raise PermissionError("publish requires an approved publish gate")
        workspace = Path(task["workspace"])
        github_token = token or os.environ.get("GITHUB_TOKEN")
        if not github_token:
            raise PermissionError(
                "GITHUB_TOKEN is required before commit, push, or draft PR creation"
            )
        if self._change_digest(workspace) != task["review"]["change_sha256"]:
            raise PermissionError("workspace changed after independent review")
        remote_base = run_command(
            [
                "git",
                "ls-remote",
                "origin",
                f"refs/heads/{task['base_branch']}",
            ],
            cwd=workspace,
            timeout=60,
        ).stdout.split()
        if not remote_base or remote_base[0] != task["source_sha"]:
            raise PermissionError("base branch moved after workspace preparation; replan")
        status = run_command(
            ["git", "status", "--porcelain"], cwd=workspace
        ).stdout.strip()
        if status:
            run_command(["git", "add", "--all"], cwd=workspace)
            run_command(
                [
                    "git",
                    "-c",
                    "user.name=Agentic Engineering",
                    "-c",
                    "user.email=agentic-engineering@users.noreply.github.com",
                    "commit",
                    "-m",
                    title,
                ],
                cwd=workspace,
            )
        head_sha = run_command(
            ["git", "rev-parse", "HEAD"], cwd=workspace
        ).stdout.strip()
        if head_sha == task["source_sha"]:
            raise ValueError("nothing to publish")
        run_command(
            ["git", "push", "--set-upstream", "origin", task["branch"]],
            cwd=workspace,
            timeout=180,
        )
        pr = self._github_request(
            "POST",
            f"/repos/{task['repository']['full_name']}/pulls",
            github_token,
            {
                "title": title,
                "body": body,
                "head": task["branch"],
                "base": task["base_branch"],
                "draft": True,
            },
        )
        task = self.store.transition(
            task,
            "published",
            actor=actor,
            reason="Approved task branch pushed and draft PR created.",
            updates={
                "pull_request": {
                    "number": pr["number"],
                    "url": pr["html_url"],
                    "draft": bool(pr["draft"]),
                    "head_sha": head_sha,
                }
            },
        )
        return self._build_evidence(task_id, actor=actor)

    @staticmethod
    def _github_request(
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Agentic-Engineering-Control-Plane",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"GitHub API failed ({exc.code}): {detail}") from exc

    @serialized_mutation
    def verify_merge(
        self,
        task_id: str,
        *,
        actor: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        task = self.store.load(task_id)
        if task["state"] != "published":
            raise ValueError(f"cannot verify merge from state: {task['state']}")
        if not task.get("pull_request"):
            raise ValueError("task has no pull request")
        github_token = token or os.environ.get("GITHUB_TOKEN")
        if not github_token:
            raise PermissionError("GITHUB_TOKEN is required for merge verification")
        full_name = task["repository"]["full_name"]
        number = task["pull_request"]["number"]
        pr = self._github_request("GET", f"/repos/{full_name}/pulls/{number}", github_token)
        if not pr.get("merged_at"):
            raise RuntimeError("pull request is not merged")
        reviewed_head = task["pull_request"].get("head_sha")
        merged_head = pr.get("head", {}).get("sha")
        if not reviewed_head or merged_head != reviewed_head:
            raise PermissionError(
                "merged pull-request head does not match the reviewed published head"
            )
        merge_sha = pr["merge_commit_sha"]
        checks = self._github_request(
            "GET", f"/repos/{full_name}/commits/{merge_sha}/check-runs", github_token
        )
        check_runs = checks.get("check_runs", [])
        by_name = {item.get("name"): item for item in check_runs}
        required_checks = self._task_required_checks(task)
        missing = [
            name
            for name in required_checks
            if name not in by_name
        ]
        if missing:
            raise RuntimeError(
                f"required post-merge checks are missing: {missing}"
            )
        incomplete = [
            item["name"]
            for item in check_runs
            if item.get("status") != "completed" or item.get("conclusion") != "success"
        ]
        if incomplete:
            raise RuntimeError(f"post-merge checks are not successful: {incomplete}")
        task = self.store.transition(
            task,
            "merged_verified",
            actor=actor,
            reason="GitHub merge and post-merge checks verified.",
            updates={
                "merge_verification": {
                    "merge_sha": merge_sha,
                    "merged_at": pr["merged_at"],
                    "check_runs": len(check_runs),
                    "required_checks": list(
                        required_checks
                    ),
                }
            },
        )
        return self._build_evidence(task_id, actor=actor)

    def monitor(self) -> dict[str, Any]:
        tasks = self.store.list_tasks()
        states: dict[str, int] = {}
        for task in tasks:
            states[task["state"]] = states.get(task["state"], 0) + 1
        return {
            "task_count": len(tasks),
            "state_counts": states,
            "event_chains_valid": all(
                self.store.verify_chain(task["task_id"]) for task in tasks
            ),
            "production_actions_enabled": False,
            "direct_default_branch_push": False,
            "draft_pr_only": True,
        }


ONBOARDING_STATES = {
    "discovered",
    "assessment_blocked",
    "awaiting_onboarding_approval",
    "approved",
    "smoke_failed",
    "active",
    "suspended",
}


class OnboardingStore:
    def __init__(self, runtime_root: str | Path) -> None:
        runtime = Path(runtime_root)
        self.root = runtime / "development" / "onboarding"
        self.lock_path = runtime / ".lifecycle-mutation.lock"
        self.root.mkdir(parents=True, exist_ok=True)

    def mutation_lock(self) -> FileMutex:
        return FileMutex(self.lock_path, timeout_seconds=660)

    @staticmethod
    def onboarding_id(full_name: str) -> str:
        return stable_id("ONB", full_name.lower())

    def record_root(self, full_name: str) -> Path:
        return self.root / self.onboarding_id(full_name)

    def state_path(self, full_name: str) -> Path:
        return self.record_root(full_name) / "state.json"

    def exists(self, full_name: str) -> bool:
        return self.state_path(full_name).is_file()

    def load(self, full_name: str) -> dict[str, Any]:
        path = self.state_path(full_name)
        if not path.is_file():
            raise KeyError(f"onboarding record not found: {full_name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, record: dict[str, Any]) -> None:
        root = self.record_root(record["full_name"])
        root.mkdir(parents=True, exist_ok=True)
        target = root / "state.json"
        if target.is_file():
            current = json.loads(target.read_text(encoding="utf-8"))
            if current.get("updated_at") != record.get("updated_at"):
                raise RuntimeError("stale onboarding record mutation rejected")
        record["updated_at"] = utc_now()
        temporary = root / "state.json.tmp"
        temporary.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, root / "state.json")

    def append_event(
        self,
        full_name: str,
        *,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        path = self.record_root(full_name) / "events.jsonl"
        previous_hash = "GENESIS"
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            if lines:
                previous_hash = json.loads(lines[-1])["event_hash"]
        event = {
            "onboarding_id": self.onboarding_id(full_name),
            "full_name": full_name,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": utc_now(),
        }
        event["event_hash"] = hashlib.sha256(
            canonical_json(event).encode("utf-8")
        ).hexdigest()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(event) + "\n")

    def events(self, full_name: str) -> list[dict[str, Any]]:
        path = self.record_root(full_name) / "events.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def verify_chain(self, full_name: str) -> bool:
        previous_hash = "GENESIS"
        for original in self.events(full_name):
            event = dict(original)
            actual = event.pop("event_hash")
            expected = hashlib.sha256(
                canonical_json(event).encode("utf-8")
            ).hexdigest()
            if event["previous_hash"] != previous_hash or actual != expected:
                return False
            previous_hash = actual
        return True

    def list(self) -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("ONB-*/state.json"))
        ]


class RepositoryOnboardingController:
    def __init__(
        self,
        *,
        policy: DevelopmentPolicy,
        registry: RepositoryRegistry,
        runtime_root: str | Path,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.store = OnboardingStore(runtime_root)

    @serialized_mutation
    def discover(
        self,
        repositories: list[dict[str, Any]],
        *,
        actor: str,
    ) -> dict[str, Any]:
        discovered = existing = ignored = 0
        records = []
        active_full_names = {
            item["full_name"].lower()
            for item in self.registry.list()
        }
        for repository in repositories:
            full_name = str(repository["full_name"])
            owner = full_name.split("/", 1)[0]
            if owner not in self.policy.allowed_owners or repository.get("archived"):
                ignored += 1
                continue
            if full_name.lower() in active_full_names:
                existing += 1
                continue
            if self.store.exists(full_name):
                record = self.store.load(full_name)
                record["metadata"] = {
                    **record["metadata"],
                    **repository,
                }
                self.store.save(record)
                existing += 1
            else:
                now = utc_now()
                record = {
                    "schema_version": "1.0.0",
                    "onboarding_id": self.store.onboarding_id(full_name),
                    "full_name": full_name,
                    "name": str(repository["name"]),
                    "state": "discovered",
                    "metadata": dict(repository),
                    "frameworks": [],
                    "proposed_test_commands": [],
                    "assessment": None,
                    "approval": None,
                    "smoke_result": None,
                    "created_at": now,
                    "updated_at": now,
                }
                self.store.save(record)
                self.store.append_event(
                    full_name,
                    event_type="repository_discovered",
                    actor=actor,
                    payload={"state": "discovered"},
                )
                discovered += 1
            records.append(record)
        return {
            "discovered": discovered,
            "existing": existing,
            "ignored": ignored,
            "records": records,
        }

    def _clone_for_assessment(self, record: dict[str, Any]) -> Path:
        root = self.store.record_root(record["full_name"])
        workspace = root / "assessment"
        if workspace.exists():
            if workspace.resolve().parent != root.resolve():
                raise PermissionError("invalid assessment workspace")
            shutil.rmtree(workspace)
        source = str(record["metadata"]["clone_url"])
        prefix = ["git"]
        local = Path(source)
        if local.exists():
            prefix.extend(
                [
                    "-c",
                    f"safe.directory={local.resolve()}",
                    "-c",
                    f"safe.directory={(local / '.git').resolve()}",
                ]
            )
        run_command(
            prefix
            + [
                "clone",
                "--no-tags",
                "--single-branch",
                "--branch",
                str(record["metadata"]["default_branch"]),
                source,
                str(workspace),
            ],
            cwd=root,
            timeout=180,
        )
        return workspace

    @staticmethod
    def _detect_contract(workspace: Path) -> tuple[list[str], list[list[str]], list[str]]:
        files: list[str] = []
        risks: list[str] = []
        base = workspace.resolve()
        for path in workspace.rglob("*"):
            if ".git" in path.parts:
                continue
            if path.is_symlink():
                risks.append("symbolic_link_present")
                continue
            if not path.is_file():
                continue
            resolved = path.resolve()
            if base not in resolved.parents:
                risks.append("path_escape_detected")
                continue
            files.append(path.relative_to(workspace).as_posix())
            if len(files) > 20000:
                raise ValueError("repository assessment file budget exceeded")
        lowered = {item.lower() for item in files}
        frameworks: list[str] = []
        commands: list[list[str]] = []

        if "scripts/validate_configs.py" in lowered:
            frameworks.append("python-config-validator")
            commands.append(["python", "scripts/validate_configs.py"])
        elif "pyproject.toml" in lowered or any(item.endswith(".py") for item in lowered):
            frameworks.append("python")
            test_files = [
                workspace / item
                for item in files
                if item.lower().startswith("tests/") and item.lower().endswith(".py")
            ]
            uses_pytest = any(
                "import pytest"
                in path.read_text(encoding="utf-8", errors="replace")
                for path in test_files[:200]
            )
            if uses_pytest:
                commands.append(["python", "-m", "pytest", "tests/", "-v"])
            elif test_files:
                commands.append(
                    ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
                )

        if "package.json" in lowered:
            frameworks.append("node")
            package = json.loads(
                (workspace / "package.json").read_text(encoding="utf-8")
            )
            test_script = package.get("scripts", {}).get("test")
            if test_script and "no test specified" not in test_script.lower():
                commands.append(["npm", "test"])

        if "go.mod" in lowered:
            frameworks.append("go")
            commands.append(["go", "test", "./..."])
        if "cargo.toml" in lowered:
            frameworks.append("rust")
            commands.append(["cargo", "test"])
        if any(item.endswith(".sln") or item.endswith(".csproj") for item in lowered):
            frameworks.append("dotnet")
            commands.append(["dotnet", "test"])

        powershell_files = [item for item in files if item.lower().endswith(".ps1")]
        if powershell_files:
            frameworks.append("powershell")
            commands.append(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$failed=$false;Get-ChildItem -Recurse -Filter *.ps1|ForEach-Object{"
                    "$e=$null;$t=$null;[System.Management.Automation.Language.Parser]"
                    "::ParseFile($_.FullName,[ref]$t,[ref]$e)|Out-Null;"
                    "if($e.Count){$failed=$true;$e|Out-String|Write-Error}};"
                    "if($failed){exit 1}",
                ]
            )

        if any(item.startswith(".github/workflows/") for item in lowered):
            risks.append("repository_contains_ci_workflows")
        if any(
            item.endswith((".tf", ".bicep"))
            or item.startswith(("terraform/", "ansible/"))
            for item in lowered
        ):
            risks.append("infrastructure_as_code_present")
        if any(
            Path(item).name.lower() in {".env", "id_rsa", "id_ed25519"}
            for item in files
        ):
            risks.append("sensitive_filename_present")
        if not commands:
            risks.append("no_executable_test_contract_detected")
        return sorted(set(frameworks)), commands, sorted(set(risks))

    @serialized_mutation
    def assess(self, full_name: str, *, actor: str) -> dict[str, Any]:
        record = self.store.load(full_name)
        if record["state"] not in {"discovered", "assessment_blocked", "smoke_failed"}:
            raise ValueError(f"cannot assess from state: {record['state']}")
        workspace = self._clone_for_assessment(record)
        source_sha = run_command(
            ["git", "rev-parse", "HEAD"], cwd=workspace
        ).stdout.strip()
        frameworks, commands, risks = self._detect_contract(workspace)
        record.update(
            {
                "frameworks": frameworks,
                "proposed_test_commands": commands,
                "assessment": {
                    "actor": actor,
                    "source_sha": source_sha,
                    "workspace": str(workspace.resolve()),
                    "risks": risks,
                    "assessed_at": utc_now(),
                },
                "approval": None,
                "smoke_result": None,
                "state": (
                    "awaiting_onboarding_approval"
                    if commands
                    else "assessment_blocked"
                ),
            }
        )
        self.store.save(record)
        self.store.append_event(
            full_name,
            event_type="repository_assessed",
            actor=actor,
            payload={
                "state": record["state"],
                "frameworks": frameworks,
                "test_commands": commands,
                "risks": risks,
            },
        )
        return record

    @serialized_mutation
    def set_contract(
        self,
        full_name: str,
        *,
        commands: list[list[str]],
        actor: str,
    ) -> dict[str, Any]:
        record = self.store.load(full_name)
        if record["state"] not in {
            "assessment_blocked",
            "awaiting_onboarding_approval",
            "smoke_failed",
        }:
            raise ValueError(f"cannot replace contract from state: {record['state']}")
        if not commands:
            raise ValueError("at least one test command is required")
        for argv in commands:
            if not isinstance(argv, list) or not argv:
                raise ValueError("test commands must be non-empty argument arrays")
            executable = Path(str(argv[0])).name.lower()
            if executable not in self.policy.allowed_test_executables:
                raise PermissionError(f"test executable is not allowed: {executable}")
        record.update(
            {
                "proposed_test_commands": commands,
                "state": "awaiting_onboarding_approval",
                "approval": None,
                "smoke_result": None,
            }
        )
        self.store.save(record)
        self.store.append_event(
            full_name,
            event_type="test_contract_replaced",
            actor=actor,
            payload={"test_commands": commands},
        )
        return record

    @serialized_mutation
    def approve(
        self,
        full_name: str,
        *,
        actor: str,
        confirmation: str,
        comment: str,
    ) -> dict[str, Any]:
        record = self.store.load(full_name)
        if record["state"] != "awaiting_onboarding_approval":
            raise ValueError(f"cannot approve from state: {record['state']}")
        if actor == record["assessment"]["actor"]:
            raise PermissionError("onboarding approver must be independent from assessor")
        expected = f"APPROVE ONBOARD {full_name}"
        if confirmation != expected:
            raise PermissionError(f"confirmation must exactly equal: {expected}")
        record.update(
            {
                "state": "approved",
                "approval": {
                    "actor": actor,
                    "confirmation": confirmation,
                    "comment": comment,
                    "approved_at": utc_now(),
                },
            }
        )
        self.store.save(record)
        self.store.append_event(
            full_name,
            event_type="onboarding_approved",
            actor=actor,
            payload={"comment": comment},
        )
        return record

    @serialized_mutation
    def activate(self, full_name: str, *, actor: str) -> dict[str, Any]:
        record = self.store.load(full_name)
        if record["state"] != "approved":
            raise ValueError(f"cannot activate from state: {record['state']}")
        if actor == record["approval"]["actor"]:
            raise PermissionError("smoke-test runner must be independent from approver")
        workspace = Path(record["assessment"]["workspace"])
        remote = run_command(
            [
                "git",
                "ls-remote",
                "origin",
                f"refs/heads/{record['metadata']['default_branch']}",
            ],
            cwd=workspace,
            timeout=60,
        ).stdout.split()
        if not remote or remote[0] != record["assessment"]["source_sha"]:
            raise PermissionError(
                "repository default branch moved after assessment; reassessment required"
            )
        results = []
        passed = True
        for argv in record["proposed_test_commands"]:
            result = execute_test_command(
                self.policy,
                argv,
                workspace=workspace,
                output_dir=self.store.record_root(full_name) / "container-output",
            )
            results.append(
                {
                    "argv": argv,
                    "exit_code": result.returncode,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                }
            )
            if result.returncode:
                passed = False
                break
        record["smoke_result"] = {
            "actor": actor,
            "passed": passed,
            "results": results,
            "completed_at": utc_now(),
        }
        if not passed:
            record["state"] = "smoke_failed"
            self.store.save(record)
            self.store.append_event(
                full_name,
                event_type="onboarding_smoke_failed",
                actor=actor,
                payload={"results": results},
            )
            return record

        metadata = record["metadata"]
        repository = {
            "name": record["name"],
            "full_name": full_name,
            "clone_url": metadata["clone_url"],
            "default_branch": metadata["default_branch"],
            "test_commands": record["proposed_test_commands"],
            "status": "active",
            "onboarded_source_sha": record["assessment"]["source_sha"],
            "onboarding_id": record["onboarding_id"],
        }
        self.registry.save_overlay(repository)
        record["state"] = "active"
        self.store.save(record)
        self.store.append_event(
            full_name,
            event_type="repository_activated",
            actor=actor,
            payload={
                "source_sha": record["assessment"]["source_sha"],
                "test_commands": record["proposed_test_commands"],
            },
        )
        return record

    @serialized_mutation
    def suspend(
        self,
        name: str,
        *,
        actor: str,
        confirmation: str,
        reason: str,
    ) -> dict[str, Any]:
        repository = self.registry.get(name)
        expected = f"SUSPEND {repository['full_name']}"
        if confirmation != expected:
            raise PermissionError(f"confirmation must exactly equal: {expected}")
        suspended = {**repository, "status": "suspended"}
        self.registry.save_overlay(suspended)
        full_name = repository["full_name"]
        if self.store.exists(full_name):
            record = self.store.load(full_name)
        else:
            now = utc_now()
            record = {
                "schema_version": "1.0.0",
                "onboarding_id": self.store.onboarding_id(full_name),
                "full_name": full_name,
                "name": repository["name"],
                "state": "active",
                "metadata": repository,
                "frameworks": [],
                "proposed_test_commands": repository.get("test_commands", []),
                "assessment": None,
                "approval": None,
                "smoke_result": None,
                "created_at": now,
                "updated_at": now,
            }
        record["state"] = "suspended"
        record["suspension"] = {
            "actor": actor,
            "reason": reason,
            "suspended_at": utc_now(),
        }
        self.store.save(record)
        self.store.append_event(
            full_name,
            event_type="repository_suspended",
            actor=actor,
            payload={"reason": reason},
        )
        return record

    @serialized_mutation
    def resume_onboarding(
        self,
        full_name: str,
        *,
        actor: str,
        confirmation: str,
        reason: str,
    ) -> dict[str, Any]:
        record = self.store.load(full_name)
        if record["state"] != "suspended":
            raise ValueError(f"cannot resume from state: {record['state']}")
        expected = f"RESUME ONBOARDING {full_name}"
        if confirmation != expected:
            raise PermissionError(f"confirmation must exactly equal: {expected}")
        record.update(
            {
                "state": "discovered",
                "assessment": None,
                "approval": None,
                "smoke_result": None,
                "resumed_by": actor,
                "resume_reason": reason,
            }
        )
        self.store.save(record)
        self.store.append_event(
            full_name,
            event_type="repository_onboarding_resumed",
            actor=actor,
            payload={"reason": reason},
        )
        return record

    def show(self, full_name: str) -> dict[str, Any]:
        record = self.store.load(full_name)
        record["events"] = self.store.events(full_name)
        record["event_chain_valid"] = self.store.verify_chain(full_name)
        return record

    def status(self) -> dict[str, Any]:
        records = self.store.list()
        states: dict[str, int] = {}
        for record in records:
            states[record["state"]] = states.get(record["state"], 0) + 1
        return {
            "active_registry_count": len(self.registry.list()),
            "onboarding_record_count": len(records),
            "state_counts": states,
            "event_chains_valid": all(
                self.store.verify_chain(record["full_name"]) for record in records
            ),
        }
