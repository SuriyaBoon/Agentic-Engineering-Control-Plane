# Verified audit baseline

Validation date: 2026-07-28 (Asia/Bangkok)

The control plane was run against the current 23-repository inventory for
`SuriyaBoon`, using local read-only sources first and GitHub snapshots for
missing public sources.

| Result | Count | Meaning |
|---|---:|---|
| Audited | 15 | Source context was scanned and findings passed the reviewer |
| Authentication required | 4 | Private repository was preserved in coverage but no read-only token was supplied |
| Unavailable | 4 | GitHub returned HTTP 451 for the repository source |

The verified run produced 53 accepted rule-backed findings:

- Critical: 0
- High: 5
- Medium: 17
- Low: 30
- Informational: 3

The evidence manifest covered 49 generated files and every SHA-256 value was
recomputed successfully. The unit suite passed 12 tests.

This baseline proves workflow execution and honest inventory coverage. It does
not certify the repositories and does not imply that unavailable/private source
was inspected. A new run should be used for current conclusions.
