# Verified audit and workflow baseline

Validation date: 2026-07-28 (Asia/Bangkok)

Run ID: `20260728T102002Z`

The control plane was run against the current 24-repository inventory for
`SuriyaBoon`, using local read-only sources first and immutable GitHub snapshots
for missing public sources.

| Result | Count | Meaning |
|---|---:|---|
| Audited | 16 | Source context was scanned and findings passed the reviewer |
| Authentication required | 4 | Private repository remained in coverage but no read-only token was supplied |
| Unavailable | 4 | GitHub returned HTTP 451 for the repository source |

The run produced 57 accepted rule-backed findings:

- Critical: 0
- High: 7
- Medium: 17
- Low: 30
- Informational: 3

All 57 findings were ingested as idempotent governed work items. One real
finding was then exercised through the complete safe lifecycle:

```text
new
-> triaged
-> planned
-> awaiting_execution_approval
-> approved
-> executing
-> validating
-> awaiting_closure_approval
-> closed
```

Distinct actors performed triage, planning, execution approval, execution,
validation, and closure approval. The executor produced a dry-run artifact; no
source repository or production system was changed.

Final workflow proof:

- Work items: 57
- Closed simulation items: 1
- New items awaiting triage: 56
- Lifecycle events: 70
- Event hash chain: valid
- Source repository mutation: false
- Production execution: false

The audit evidence manifest covered 51 generated files and every SHA-256 value
was recomputed successfully. The unit suite passed 24 tests.

This baseline proves workflow execution and honest inventory coverage. It does
not certify the repositories and does not imply that unavailable/private source
was inspected or that simulated findings were remediated in source.
