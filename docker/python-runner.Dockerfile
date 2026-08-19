FROM docker.io/library/python@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a

LABEL org.opencontainers.image.title="Agentic Engineering Python Test Runner"
LABEL org.opencontainers.image.description="Digest-published Phase 0 Python and pytest test runner"

COPY --chown=0:0 --chmod=0444 docker/python-runner-requirements.txt /opt/ae-runner/requirements.txt

RUN python -m pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      --only-binary=:all: \
      --require-hashes \
      --no-deps \
      --index-url=https://pypi.org/simple \
      --requirement /opt/ae-runner/requirements.txt \
    && python -m pip check \
    && python -m pytest --version

ENV HOME=/tmp \
    TMPDIR=/tmp \
    PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
WORKDIR /workspace

