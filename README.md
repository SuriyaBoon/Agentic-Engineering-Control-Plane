# Agentic Engineering Control Plane

A standalone, governed protocol for controlling development across registered
[`SuriyaBoon`](https://github.com/SuriyaBoon) repositories. It also retains the
read-only portfolio audit and governed finding workflow from version 0.2.

Version 0.6 combines dynamic repository onboarding with intent/risk
classification, planning, agent routing, immutable
source identity, isolated clone/branch workspaces, bounded text change sets,
registered test contracts, independent review, acceptance evidence, explicit
human publish approval, task-branch push, draft pull requests, post-merge check
verification, snapshot evidence packages, serialized lifecycle mutation,
hash-chained memory, and monitoring.

The original source checkout remains read-only. Changes occur only in a
per-task isolated clone. Direct default-branch pushes, automatic merge,
deployment, and production infrastructure actions are prohibited.

## Verified scope

- Inventory snapshot: 24 repositories (20 public, 4 private).
- Public repositories can be audited without credentials.
- Private repositories require a read-only `GITHUB_TOKEN`; otherwise they are
  recorded as `auth_required`, never silently omitted.
- Eight audit agents plus an evidence reviewer.
- Governed work-item state machine with SLA, bounded retry, and manual fallback.
- Independent execution approval, executor, validator, and closure approver.
- SQLite operational memory with an append-only SHA-256 event chain.
- Ten registered ecosystem repositories with source-verified test or static
  validation entry points.
- Dynamic GitHub discovery with quarantine, deterministic framework/test
  assessment, independent onboarding approval, isolated smoke validation,
  runtime activation, and suspension.
- Generated `agentic/DEV-*` branches and immutable source SHA capture.
- File/line/size/retry/test-executable budgets and path traversal protection.
- Independent test, review, secret scan, and acceptance-evidence gates.
- Exact human approval required before a task-branch push and draft PR.
- Reviewed-diff and base-branch drift checks before publication.
- GitHub merge verification requires the exact reviewed PR head SHA and every
  policy-required post-merge check; no merge operation exists.
- Versioned evidence packages preserve state/event snapshots and verify every
  artifact hash before the live lifecycle records the manifest.
- A cross-process file mutex serializes development and onboarding mutations;
  stale task/record writes fail closed.
- Production actions remain disabled.
- Deterministic operation without an LLM or third-party Python package.

## Development control flow

```mermaid
flowchart LR
    U["User request"] --> I["Intent and risk"]
    I --> P["Plan and agent routing"]
    P --> W["Isolated clone and task branch"]
    W --> C["Coder change set"]
    C --> T["Independent tests"]
    T --> R["Independent review"]
    R --> H{"Human publish approval"}
    H -- "Rejected" --> M["Manual review"]
    H -- "Approved" --> D["Commit and task-branch push"]
    D --> PR["Draft pull request"]
    PR --> HM["Human merge"]
    HM --> V["Post-merge checks"]
    V --> E["Evidence and monitoring"]
```

This is a development protocol outside the target repositories. SentinelGRC
remains the ecosystem's security-governance application; it is simply one
possible target of this development controller.

See [Governed cross-repository development protocol](docs/DEVELOPMENT-PROTOCOL.md)
for inputs, outputs, decisions, roles, retry/fallback behavior, metrics,
guardrails, commands, and pseudocode.

## Read-only audit and finding flow

```mermaid
flowchart LR
    U["User input"] --> I["Intent and policy guard"]
    I --> A["Repository inventory and immutable source"]
    A --> D["Specialist audit agents"]
    D --> R["Evidence reviewer"]
    R --> W["Finding to governed work item"]
    W --> T["Triage, owner, severity, SLA"]
    T --> P["Remediation planner"]
    P --> G{"Human execution approval"}
    G -- "Rejected" --> M["Manual review"]
    G -- "Approved" --> X["Safe dry-run executor"]
    X --> V["Independent validation"]
    V -- "Failed" --> Y{"Retry budget available"}
    Y -- "Yes" --> X
    Y -- "No" --> M
    V -- "Passed" --> C{"Independent closure approval"}
    C -- "Rejected" --> M
    C -- "Approved" --> Z["Close simulated work item"]
    Z --> E["Hash-chained memory"]
    E --> O["Response and monitoring"]
```

## Audit workflow mapping

| Stage | Implementation | Output |
|---|---|---|
| User Input | CLI commands and explicit actor identities | Validated command |
| Intent Understanding | Policy guard and allowed mode checks | Allowed/denied intent |
| Task Planning | `RemediationPlanner` | Versioned remediation plan |
| Agent Routing | Audit agent selection plus lifecycle role routing | Assigned agent/role |
| Tool Selection | `ToolRegistry` with enabled and disabled boundaries | Safe tool decision |
| Execution | `SafeExecutor` in `dry_run`/`mock` only | Hashed execution artifact |
| Validation | Evidence reviewer and independent execution validator | Pass/fail with reasons |
| Memory Update | SQLite state plus SHA-256 chained events | Tamper-evident history |
| Response Generation | JSON/Markdown reports and CLI responses | Human-readable evidence |
| Monitoring | State, severity, overdue, retry, and integrity metrics | Operational status |

## State machine

```mermaid
stateDiagram-v2
    [*] --> new
    new --> triaged
    triaged --> planned
    planned --> awaiting_execution_approval
    awaiting_execution_approval --> approved: independent approval
    awaiting_execution_approval --> manual_review: rejected
    approved --> executing
    executing --> validating: artifact generated
    executing --> approved: bounded retry
    executing --> manual_review: retry exhausted
    validating --> awaiting_closure_approval: passed
    validating --> approved: validation retry
    validating --> manual_review: retry exhausted
    awaiting_closure_approval --> closed: independent approval
    awaiting_closure_approval --> manual_review: rejected
    manual_review --> triaged
    manual_review --> planned
    manual_review --> approved
    manual_review --> failed
    failed --> triaged
    closed --> [*]
```

`closed` means the governed **simulation work item** completed with verified
evidence. It does not assert that the source repository was changed or that the
underlying production risk was remediated.

## Roles

| Role/agent | Responsibility | Separation rule |
|---|---|---|
| Intent guard | Reject policy-violating requests | Fails closed |
| Audit agents | Detect architecture, workflow, security, test, CI, documentation, integration, and governance gaps | Read-only |
| Evidence reviewer | Reject findings without scanned-file or metadata evidence | Independent from detectors |
| Triage lead | Confirm owner and SLA | Cannot close work |
| Planner | Produce steps, tool, validation, retry, and fallback | Cannot approve own plan |
| Execution approver | Approve/reject execution | Cannot execute |
| Safe executor | Generate a dry-run artifact | Cannot validate own output |
| Validator | Verify artifact/hash/guardrails | Cannot close work |
| Closure approver | Approve/reject closure | Must not be a prior lifecycle actor |
| Memory/monitor | Record hash-chained history and operational metrics | No mutation authority |

## Quick start

Requires Python 3.11 or newer.

```powershell
python -m ae_control_plane.cli doctor
python -m ae_control_plane.cli agents
python -m ae_control_plane.cli repo-list
python -m ae_control_plane.cli repo-onboarding-monitor
python -B -m unittest discover -v -s tests
```

Discover future repositories without granting development authority:

```powershell
python -m ae_control_plane.cli repo-sync `
  --owner SuriyaBoon --actor discovery-agent
```

New repositories remain quarantined until `repo-assess`,
`repo-onboard-approve`, and `repo-activate` all succeed.

Start a governed development task:

```powershell
python -m ae_control_plane.cli dev-start `
  --repository SentinelGRC `
  --intent "Add a contract test for security_alert.v1" `
  --acceptance "The fixture is validated" `
  --acceptance "The existing suite remains green" `
  --actor requester
```

Continue through `dev-plan`, `dev-prepare`, `dev-apply`, `dev-test`,
`dev-review`, `dev-evidence`, and `dev-approve`. Only after the owner records
the exact approval may `dev-publish` push the generated branch and create a
draft PR. `dev-verify-merge` closes the loop after a human merge and successful
post-merge checks. The complete command sequence is in
[`docs/DEVELOPMENT-PROTOCOL.md`](docs/DEVELOPMENT-PROTOCOL.md).

### Phase 0 Docker isolation

Untrusted Python repository tests run through a fail-closed Docker adapter.
The policy pins the runner by SHA-256 digest and disables network access,
mounts the isolated workspace read-only, runs as a numeric non-root user,
drops capabilities, enables `no-new-privileges`, and applies PID, CPU, memory,
temporary-storage, and wall-clock limits. Docker or image unavailability and
unsupported runtimes are errors; the runner never silently falls back to host
execution.

This is a production-shaped concept boundary, not a complete multi-tenant
sandbox. See [`docs/PHASE-0-THREAT-MODEL.md`](docs/PHASE-0-THREAT-MODEL.md)
for assumptions, adversarial tests, limitations, and the live validation
command.

Audit every available repository and create governed work items:

```powershell
python -m ae_control_plane.cli audit-all `
  --source-root C:\path\to\repositories `
  --download-missing `
  --live-inventory `
  --create-work-items
```

An existing audit run can be ingested idempotently:

```powershell
python -m ae_control_plane.cli workflow-ingest `
  --run-root C:\path\to\runs\<run-id>
```

## Governed lifecycle commands

Use distinct human/agent actor names. The system enforces separation of duties.

```powershell
python -m ae_control_plane.cli work-list --state new

python -m ae_control_plane.cli work-triage `
  --work-item-id WORK-... `
  --actor triage-lead `
  --owner repository-owner

python -m ae_control_plane.cli work-plan `
  --work-item-id WORK-... `
  --actor remediation-planner

python -m ae_control_plane.cli work-approve `
  --work-item-id WORK-... `
  --stage execution `
  --decision approved `
  --actor change-approver `
  --comment "Reviewed for dry-run execution"

python -m ae_control_plane.cli work-execute `
  --work-item-id WORK-... `
  --actor safe-executor

python -m ae_control_plane.cli work-validate `
  --work-item-id WORK-... `
  --actor independent-validator

python -m ae_control_plane.cli work-approve `
  --work-item-id WORK-... `
  --stage closure `
  --decision approved `
  --actor risk-owner `
  --comment "Simulation evidence independently verified"

python -m ae_control_plane.cli work-show --work-item-id WORK-...
python -m ae_control_plane.cli monitor
```

## Private repositories

Create a fine-grained token with read-only Contents and Metadata access only:

```powershell
$env:GITHUB_TOKEN = "<read-only fine-grained token>"
python -m ae_control_plane.cli audit-all `
  --download-missing `
  --live-inventory `
  --create-work-items
Remove-Item Env:GITHUB_TOKEN
```

The token is never written to reports or workflow memory.

## Runtime outputs

Runtime data defaults to `%TEMP%\agentic-engineering-control-plane`:

```text
runs/<UTC-run-id>/
|-- inventory.json
|-- manifest.json
|-- portfolio.json
|-- portfolio.md
`-- repositories/
    |-- SuriyaBoon__Example.json
    `-- SuriyaBoon__Example.md

workflow/
|-- workflow.db
`-- executions/
    `-- WORK-.../
        `-- attempt-1.json

development/tasks/DEV-.../
|-- state.json
|-- events.jsonl
`-- evidence/EVD-.../
    |-- state.json
    |-- events.jsonl
    `-- manifest.json
```

The evidence package contains immutable snapshots. The live `state.json` and
`events.jsonl` then record the manifest path and SHA-256, avoiding a circular or
immediately stale manifest hash.

## Retry and fallback

- Maximum attempts are configured in `config/policy.json`.
- Execution or validation failure returns to `approved` while budget remains.
- Exhausted attempts move to `manual_review`.
- Rejected execution or closure also moves to `manual_review`.
- Invalid state transitions, self-approval, missing evidence, disabled tools,
  artifact tampering, and source-mutation plans fail closed.

## Monitoring

`monitor` returns:

- work items by state and severity;
- overdue count and identifiers;
- event count and event-chain integrity;
- explicit `production_execution=false`;
- explicit `source_repository_mutation=false`.

The CLI is suitable for a scheduler or CI job, but the repository does not
install a background service or silently schedule work on the user's machine.

## Safety boundary

The following tools exist only as disabled boundaries:

- `github_draft_pr`
- `live_infrastructure_action`

The active `mock_remediation` tool writes a proposal artifact outside source
repositories. It cannot commit, push, create PRs/issues, deploy, modify AD/VMs,
perform restore operations, accept organizational risk, or claim production
remediation.

Repository content is treated as untrusted data rather than instructions.
Snapshot paths and compressed/uncompressed sizes are validated, and scans are
bounded by file and byte limits.

## Validation

```powershell
python -B -m unittest discover -v -s tests
python -m ae_control_plane.cli doctor
python -m ae_control_plane.cli monitor
```

CI runs all tests and parses every committed JSON document.

See [`docs/AGENTIC-WORKFLOW.md`](docs/AGENTIC-WORKFLOW.md) for decision rules,
inputs/outputs, approval points, error handling, metrics, and pseudocode.
