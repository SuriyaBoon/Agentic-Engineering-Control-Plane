from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def git_commit_sha(root: str | Path) -> str | None:
    """Return HEAD without changing the repository or global Git configuration."""
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={Path(root).resolve()}", "rev-parse", "HEAD"],
            cwd=Path(root),
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    value = result.stdout.strip()
    if result.returncode == 0 and len(value) == 40:
        return value
    return None


def source_tree_sha256(root: str | Path, files: tuple[str, ...]) -> str:
    """Build a stable content identity for a non-Git source tree."""
    base = Path(root)
    digest = hashlib.sha256()
    for relative in sorted(files):
        path = base / relative
        if not path.is_file():
            continue
        digest.update(relative.replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return f"tree-sha256:{digest.hexdigest()}"
