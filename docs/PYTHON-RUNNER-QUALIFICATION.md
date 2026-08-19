# Python test-runner qualification protocol

The Phase 0 Python runner must include every test framework required by an
active repository before untrusted tests execute. Runtime dependency downloads
remain forbidden because test containers run with `--network none`.

## Qualification-only boundary

This repository builds and qualifies a candidate runner, but it does not publish
packages, push container images, deploy infrastructure, or activate policy.
Those external side effects remain outside the Agentic Engineering Control
Plane repository.

The manual `qualify-python-runner` workflow:

1. Builds one candidate from the digest-pinned Python base.
2. Installs only exact-version, hash-locked binary dependencies.
3. Runs pytest inside the non-root, network-disabled, read-only container.
4. Saves that exact image as `runner-image.tar`.
5. Records the archive checksum, image ID, config digest, and target platform.
6. Emits `python-runner-qualification.json` with
   `publication_performed: false` and `policy_activated: false`.
7. Uploads the checksummed qualification bundle as a short-lived GitHub
   artifact.

The workflow has read-only repository permission. It has no package-write
permission, registry login, image push, protected deployment environment, or
activation step.

## Human-owned external publication boundary

If an owner later decides to publish the qualified runner, that must occur in a
separate human-owned release process outside this repository. The release
operator must independently verify the archive checksum, source SHA, image ID,
qualified config digest, and target platform before publication.

Any external release must record the resulting immutable registry digest and
prove that its remote config digest equals the qualified config digest. A tag is
not activation evidence.

Updating `config/development-policy.json` to use a published runner requires a
separate governed activation task, explicit human approval, an exact digest,
and live isolation evidence. Qualification alone never authorizes publication
or activation.

## Remaining trust boundary

The candidate is hash-locked but not remotely signed or attested. GitHub-hosted
runners, PyPI availability during the build, the pinned Python base image,
pinned GitHub Actions, Docker, and the repository owner remain trusted
dependencies. Runtime tests still use Docker Desktop rather than a microVM or
hardened multi-tenant sandbox.
