from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from .models import RepositoryContext, RepositoryDescriptor
from .policy import AuditPolicy


TEXT_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".conf",
    ".cs",
    ".css",
    ".csv",
    ".env",
    ".go",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".psd1",
    ".psm1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

SOURCE_EXTENSIONS = {
    ".bat",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".ps1",
    ".psm1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
}

EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    "runtime",
}

MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "go.mod",
    "cargo.toml",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
}


class ContextBuilder:
    def __init__(self, policy: AuditPolicy) -> None:
        self.policy = policy

    def build(
        self,
        descriptor: RepositoryDescriptor,
        root: str | Path,
    ) -> RepositoryContext:
        repository_root = Path(root).resolve()
        if not repository_root.is_dir():
            raise ValueError(f"repository source is not a directory: {repository_root}")

        files: list[str] = []
        text_files: dict[str, str] = {}
        extensions: Counter[str] = Counter()
        total_text_bytes = 0
        truncated = False

        for current_root, directory_names, file_names in os.walk(repository_root):
            directory_names[:] = [
                name
                for name in directory_names
                if name.lower() not in EXCLUDED_DIRECTORIES
            ]
            for file_name in sorted(file_names):
                path = Path(current_root) / file_name
                relative = path.relative_to(repository_root).as_posix()
                files.append(relative)
                extensions[path.suffix.lower() or "<none>"] += 1
                if len(files) >= self.policy.max_repository_files:
                    truncated = True
                    break
                if path.suffix.lower() not in TEXT_EXTENSIONS and file_name.lower() not in MANIFEST_NAMES:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > self.policy.max_text_file_bytes:
                    continue
                if total_text_bytes + size > self.policy.max_total_text_bytes:
                    truncated = True
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                text_files[relative] = content
                total_text_bytes += size
            if truncated and len(files) >= self.policy.max_repository_files:
                break

        normalized = tuple(sorted(files))
        test_files = tuple(
            path
            for path in normalized
            if (
                path.lower().startswith(("test/", "tests/"))
                or "/test/" in path.lower()
                or "/tests/" in path.lower()
                or Path(path).name.lower().startswith("test_")
                or Path(path).name.lower().endswith(("_test.py", ".test.js", ".spec.ts"))
            )
        )
        workflow_files = tuple(
            path
            for path in normalized
            if path.lower().startswith(".github/workflows/")
            and path.lower().endswith((".yml", ".yaml"))
        )
        manifest_files = tuple(
            path for path in normalized if Path(path).name.lower() in MANIFEST_NAMES
        )
        source_files = tuple(
            path for path in normalized if Path(path).suffix.lower() in SOURCE_EXTENSIONS
        )
        return RepositoryContext(
            descriptor=descriptor,
            root=repository_root,
            files=normalized,
            text_files=text_files,
            extensions=dict(sorted(extensions.items())),
            test_files=test_files,
            workflow_files=workflow_files,
            manifest_files=manifest_files,
            source_files=source_files,
            truncated=truncated,
        )
