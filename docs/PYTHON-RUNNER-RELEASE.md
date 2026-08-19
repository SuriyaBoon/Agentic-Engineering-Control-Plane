# Python test-runner release protocol

The Phase 0 Python runner must include every test framework required by an
active repository before untrusted tests execute. Runtime dependency downloads
are forbidden because test containers run with `--network none`.

## Why the old runner fails

The current policy pins the upstream Python base image directly. The repository
registry invokes pytest for several active repositories, but that base image
does not contain pytest. A missing framework therefore fails closed before any
repository test is collected. Opening container networking or installing an
unlocked package at runtime would weaken the isolation boundary and is not an
accepted repair.

## Two-stage release

1. Merge the runner-source change after review.
2. Configure the `python-runner-release` GitHub Environment with required
   reviewers. Do not dispatch publication while that protection is absent.
3. Manually dispatch `publish-python-runner` from `main` only.
4. Verify the qualification job ran without package-publication permission.
5. Retain `python-runner-release.json` and independently inspect its source SHA,
   image ID, digest, and `policy_activated: false` boundary.
6. If the GHCR package is private, authenticate Docker to GHCR using a bounded
   package-read credential. Pull the exact `activation_reference` by digest.
7. Open a separate governed activation task that changes the policy and its
   exact-digest tests. Never activate the run-bound tag.
8. Run the live isolation suite and at least one registered pytest contract
   against that exact `activation_reference` digest. Attach the exact digest to
   the activation evidence before approving activation.

The publication workflow builds once, qualifies that exact local image, saves
it as a checksummed artifact, reloads and verifies the same image ID in the
privileged job, pushes a run-bound tag, and queries the resulting registry
digest. It does not update Control Plane policy automatically.

## Remaining trust boundary

The image is hash-locked and digest-addressed but is not signed or remotely
attested. GitHub-hosted runners, GHCR, PyPI availability during the build, the
pinned base image, pinned GitHub Actions, and the repository owner remain
trusted dependencies. Runtime tests still use Docker Desktop rather than a
microVM or hardened multi-tenant sandbox.
