FROM docker.io/library/python@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a

LABEL org.opencontainers.image.title="Agentic Engineering Python Test Runner"
LABEL org.opencontainers.image.description="Digest-pinned Phase 0 test runner"

ENV HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /workspace
