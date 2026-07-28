# Governed Agentic Engineering workflow

## Decision contract

| Step | Agent/role | Input | Output | Decision |
|---|---|---|---|---|
| User input | CLI | Command, paths, actor | Parsed request | Reject missing required arguments |
| Intent | Intent guard | Request and policy | Allowed intent | Reject mutation/live action |
| Inventory | Orchestrator | Owner and inventory | Repository descriptor | Preserve unavailable/private entries |
| Planning | Planner | Accepted finding | Remediation plan | Select enabled no-side-effect tool |
| Routing | Orchestrator | Plan and state | Next role | Enforce state transition |
| Tool selection | Tool registry | Tool name | Tool specification | Reject unknown/disabled/side-effect tool |
| Execution | Safe executor | Approved plan | JSON artifact plus SHA-256 | Require independent approval |
| Validation | Validator | Execution artifact | Pass/fail reasons | Reject tampered/unsafe artifact |
| Memory | Workflow store | Every lifecycle event | Hash-chained event | Detect chain tampering |
| Response | CLI/report writer | Current state and evidence | JSON/Markdown | Never claim production remediation |
| Monitoring | Monitor | Work items and events | State/SLA/integrity metrics | Surface overdue/manual-review work |

## Human approvals

Execution approval is mandatory for every plan, including dry-run plans. The
approver cannot be the finding ingester or planner. The executor cannot be the
execution approver. Validation must use another actor. Closure requires a
further actor who did not ingest, plan, approve execution, or execute.

An approval is an accountable decision record, not merely a state change. Actor,
stage, decision, comment, and timestamp are stored in the event memory.

## Error handling

1. Invalid input or transition fails immediately without changing state.
2. Tool selection fails when a tool is unknown, disabled, side-effecting, or
   requests a mode other than `dry_run`/`mock`.
3. Execution errors consume one bounded attempt.
4. Validation checks artifact existence, SHA-256, JSON validity, mutation guard,
   production guard, and expected simulation status.
5. A retry returns the work item to `approved`.
6. Exhausted retry budget or rejected approval moves it to `manual_review`.
7. Every decision and failure is appended to the hash chain.

## Metrics

- inventory/audit coverage;
- findings by severity;
- work items by state and severity;
- SLA due time and overdue count;
- retries and last error;
- approvals by stage and actor;
- execution artifact identity;
- event count and hash-chain validity;
- explicit mutation and production-execution flags.

## Pseudocode

```text
request = parse_cli(user_input)
policy.require_safe_intent(request)

audit_run = audit_all_repositories(request.inventory)
for reviewed_finding in audit_run:
    work = memory.create_idempotently(reviewed_finding)
    work = triage(work, owner, sla)
    plan = planner.create(work)
    tool = registry.require_enabled_no_side_effect(plan.tool)

    execution_approval = require_independent_human(plan)
    if execution_approval.rejected:
        transition(work, MANUAL_REVIEW)
        continue

    while attempts < max_retries:
        artifact = safe_executor.dry_run(plan)
        validation = independent_validator.verify(artifact)
        memory.append(artifact, validation)
        if validation.passed:
            break

    if not validation.passed:
        transition(work, MANUAL_REVIEW)
        continue

    closure = require_independent_closure_approval(work, artifact)
    transition(work, CLOSED if closure.approved else MANUAL_REVIEW)

monitor.verify_event_chain()
monitor.report_state_severity_sla_and_failures()
```

## Production extension boundary

A production extension would require a separately reviewed connector, isolated
branch workspace, repository allowlist, change budget, test contract, rollback,
draft-PR-only behavior, authenticated actor mapping, webhook verification, and
post-merge source re-audit. None of those permissions are inferred from the
safe workflow.
