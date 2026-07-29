# Phase 0 threat model

## Purpose

Phase 0 reduces the risk that repository-controlled tests, generated change
sets, or malicious instructions escape the governed development boundary. It
does not claim that Docker Desktop is equivalent to a hardened multi-tenant
sandbox.

## Trust boundaries

The requester, planner, coder, test runner, independent reviewer, repository
owner, publisher, and post-merge verifier are recorded as separate actors.
Actor names are evidence labels in this concept implementation; production
identity federation and signed workload identity remain future controls.

The source repository, its tests, fixtures, and instructions are untrusted.
The Control Plane policy, task state, event chain, human approvals, GitHub
branch protections, container engine, and host operating system are trusted
dependencies.

Lifecycle mutations are serialized with an OS-backed cross-process file mutex,
and stale state objects are rejected. Evidence generation snapshots task state
and its hash-chained events into a versioned package, verifies the package
hashes, and only then anchors the manifest SHA-256 in the live lifecycle.

## Enforced container controls

Python test contracts use the digest-pinned image recorded in
`config/development-policy.json`. The runner refuses mutable tags. Every test
container is created with:

- no network namespace connectivity;
- a read-only root filesystem;
- the isolated Git workspace mounted read-only at `/workspace`;
- a separate writable evidence/output directory at `/output`;
- a bounded, non-executable temporary filesystem at `/tmp`;
- numeric non-root UID and GID `65532:65532`;
- all Linux capabilities dropped;
- `no-new-privileges`;
- disabled IPC sharing;
- PID, memory, CPU, and wall-clock limits; and
- forced container removal after a timeout.

The runner checks Docker daemon and pinned-image availability before executing.
Missing Docker, a missing image, unsupported executables, unsafe paths, or
invalid policy values fail closed. There is no automatic fallback to host
execution. Explicit host mode exists only for trusted unit-test fixtures.

## Adversarial validation

`tests/test_isolation.py` verifies policy validation and exact container command
construction. Its optional live suite verifies:

- the process is non-root;
- writes to `/workspace` fail;
- outbound network access fails;
- `/output` remains writable; and
- timed-out containers are forcibly removed.

Run the live tests only on a machine with Docker Desktop and the pinned image:

```powershell
$env:AE_RUN_DOCKER_TESTS = "1"
python -m unittest tests.test_isolation -v
```

## Deliberate limitations

Phase 0 does not provide a separate VM, microVM, seccomp profile, AppArmor or
SELinux policy, rootless Docker daemon, signed image verification, remote
attestation, or multi-tenant workload isolation. Docker Desktop still shares a
host-managed Linux VM and daemon. A container-engine vulnerability could cross
this boundary.

Only Python test contracts are supported by the Phase 0 Docker adapter.
PowerShell, Node.js, Go, Rust, and other contracts fail closed until a
separately pinned and tested runner is configured for each runtime.

The default runtime remains under the host temporary directory. File locking
prevents concurrent writers but is not a transactional database and does not
make temporary storage durable across host cleanup or catastrophic power loss.
GitHub branch protection is a required trusted dependency but is configured
outside this repository and must be verified independently.

GitHub remains responsible for CI. Humans remain responsible for publish
approval and merge. The Control Plane cannot push a default branch, merge a
pull request, deploy software, operate production infrastructure, or accept
risk automatically.
