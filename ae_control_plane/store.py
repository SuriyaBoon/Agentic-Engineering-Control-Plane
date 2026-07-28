from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .lifecycle import (
    ALLOWED_TRANSITIONS,
    Approval,
    ExecutionResult,
    RemediationPlan,
    WorkItem,
    canonical_json,
    utc_now,
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class WorkflowStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_items (
                    work_item_id TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    source_commit TEXT,
                    source_run_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner TEXT,
                    created_by TEXT NOT NULL,
                    plan_id TEXT,
                    due_at TEXT,
                    retry_count INTEGER NOT NULL,
                    max_retries INTEGER NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(repository, finding_id, source_commit)
                );
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_item_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id)
                );
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_item_id TEXT,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_items_state
                    ON work_items(state);
                CREATE INDEX IF NOT EXISTS idx_events_work_item
                    ON events(work_item_id, sequence);
                """
            )

    @staticmethod
    def _row_to_work_item(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            work_item_id=row["work_item_id"],
            finding_id=row["finding_id"],
            repository=row["repository"],
            source_commit=row["source_commit"],
            source_run_id=row["source_run_id"],
            title=row["title"],
            category=row["category"],
            severity=row["severity"],
            recommendation=row["recommendation"],
            evidence=list(json.loads(row["evidence_json"])),
            state=row["state"],
            owner=row["owner"],
            created_by=row["created_by"],
            plan_id=row["plan_id"],
            due_at=row["due_at"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def append_event(
        self,
        *,
        work_item_id: str | None,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        connection: sqlite3.Connection | None = None,
    ) -> str:
        owns_connection = connection is None
        database = connection or self._connect()
        try:
            previous = database.execute(
                "SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = previous["event_hash"] if previous else "GENESIS"
            created_at = utc_now()
            material = canonical_json(
                {
                    "work_item_id": work_item_id,
                    "event_type": event_type,
                    "actor": actor,
                    "payload": payload,
                    "previous_hash": previous_hash,
                    "created_at": created_at,
                }
            )
            event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            database.execute(
                """
                INSERT INTO events (
                    work_item_id, event_type, actor, payload_json,
                    previous_hash, event_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_item_id,
                    event_type,
                    actor,
                    canonical_json(payload),
                    previous_hash,
                    event_hash,
                    created_at,
                ),
            )
            if owns_connection:
                database.commit()
            return event_hash
        finally:
            if owns_connection:
                database.close()

    def create_work_item(self, item: WorkItem) -> tuple[WorkItem, bool]:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO work_items (
                        work_item_id, finding_id, repository, source_commit,
                        source_run_id, title, category, severity, recommendation,
                        evidence_json, state, owner, created_by, plan_id, due_at,
                        retry_count, max_retries, last_error, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        item.work_item_id,
                        item.finding_id,
                        item.repository,
                        item.source_commit,
                        item.source_run_id,
                        item.title,
                        item.category,
                        item.severity,
                        item.recommendation,
                        json.dumps(item.evidence, sort_keys=True),
                        item.state,
                        item.owner,
                        item.created_by,
                        item.plan_id,
                        item.due_at,
                        item.retry_count,
                        item.max_retries,
                        item.last_error,
                        item.created_at,
                        item.updated_at,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM work_items
                    WHERE repository = ? AND finding_id = ?
                      AND COALESCE(source_commit, '') = COALESCE(?, '')
                    """,
                    (item.repository, item.finding_id, item.source_commit),
                ).fetchone()
                if existing is None:
                    raise
                return self._row_to_work_item(existing), False
            self.append_event(
                work_item_id=item.work_item_id,
                event_type="work_item_created",
                actor=item.created_by,
                payload={"state": item.state, "severity": item.severity},
                connection=connection,
            )
        return item, True

    def get_work_item(self, work_item_id: str) -> WorkItem:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"work item not found: {work_item_id}")
        return self._row_to_work_item(row)

    def list_work_items(self, state: str | None = None) -> list[WorkItem]:
        query = "SELECT * FROM work_items"
        parameters: tuple[Any, ...] = ()
        if state:
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY severity, due_at, work_item_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._row_to_work_item(row) for row in rows]

    def transition(
        self,
        work_item_id: str,
        target_state: str,
        *,
        actor: str,
        reason: str,
        updates: dict[str, Any] | None = None,
    ) -> WorkItem:
        item = self.get_work_item(work_item_id)
        if target_state not in ALLOWED_TRANSITIONS[item.state]:
            raise ValueError(f"invalid transition: {item.state} -> {target_state}")
        allowed_updates = {
            "owner",
            "plan_id",
            "retry_count",
            "last_error",
            "due_at",
        }
        changes = dict(updates or {})
        invalid = set(changes) - allowed_updates
        if invalid:
            raise ValueError(f"unsupported work item updates: {sorted(invalid)}")
        changes["state"] = target_state
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        parameters = [changes[key] for key in changes]
        parameters.append(work_item_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE work_items SET {assignments} WHERE work_item_id = ?",
                parameters,
            )
            self.append_event(
                work_item_id=work_item_id,
                event_type="state_transition",
                actor=actor,
                payload={
                    "from": item.state,
                    "to": target_state,
                    "reason": reason,
                    "updates": updates or {},
                },
                connection=connection,
            )
        return self.get_work_item(work_item_id)

    def save_plan(self, plan: RemediationPlan) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO plans(plan_id, work_item_id, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.work_item_id,
                    canonical_json(plan.to_dict()),
                    plan.created_at,
                ),
            )
            self.append_event(
                work_item_id=plan.work_item_id,
                event_type="plan_created",
                actor=plan.planner,
                payload=plan.to_dict(),
                connection=connection,
            )

    def get_plan(self, work_item_id: str) -> RemediationPlan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM plans WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"plan not found for work item: {work_item_id}")
        return RemediationPlan.from_dict(json.loads(row["payload_json"]))

    def add_approval(self, approval: Approval) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    work_item_id, stage, actor, decision, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.work_item_id,
                    approval.stage,
                    approval.actor,
                    approval.decision,
                    approval.comment,
                    approval.created_at,
                ),
            )
            self.append_event(
                work_item_id=approval.work_item_id,
                event_type=f"{approval.stage}_approval",
                actor=approval.actor,
                payload=approval.to_dict(),
                connection=connection,
            )

    def approvals(self, work_item_id: str) -> list[Approval]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE work_item_id = ? ORDER BY approval_id",
                (work_item_id,),
            ).fetchall()
        return [
            Approval(
                work_item_id=row["work_item_id"],
                stage=row["stage"],
                actor=row["actor"],
                decision=row["decision"],
                comment=row["comment"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_execution(self, result: ExecutionResult) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO executions(
                    execution_id, work_item_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    result.execution_id,
                    result.work_item_id,
                    canonical_json(result.to_dict()),
                    result.created_at,
                ),
            )
            self.append_event(
                work_item_id=result.work_item_id,
                event_type="execution_recorded",
                actor=result.actor,
                payload=result.to_dict(),
                connection=connection,
            )

    def latest_execution(self, work_item_id: str) -> ExecutionResult:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM executions
                WHERE work_item_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (work_item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"execution not found for work item: {work_item_id}")
        return ExecutionResult(**json.loads(row["payload_json"]))

    def events(self, work_item_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        parameters: tuple[Any, ...] = ()
        if work_item_id:
            query += " WHERE work_item_id = ?"
            parameters = (work_item_id,)
        query += " ORDER BY sequence"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "work_item_id": row["work_item_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "payload": json.loads(row["payload_json"]),
                "previous_hash": row["previous_hash"],
                "event_hash": row["event_hash"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def verify_event_chain(self) -> bool:
        previous_hash = "GENESIS"
        for event in self.events():
            material = canonical_json(
                {
                    "work_item_id": event["work_item_id"],
                    "event_type": event["event_type"],
                    "actor": event["actor"],
                    "payload": event["payload"],
                    "previous_hash": previous_hash,
                    "created_at": event["created_at"],
                }
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if event["previous_hash"] != previous_hash or event["event_hash"] != expected:
                return False
            previous_hash = event["event_hash"]
        return True
