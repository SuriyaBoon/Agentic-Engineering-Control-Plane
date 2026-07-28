from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import RepositoryDescriptor


class GitHubAccessError(RuntimeError):
    pass


class GitHubClient:
    API_ROOT = "https://api.github.com"

    def __init__(
        self,
        *,
        token: str | None = None,
        user_agent: str = "agentic-engineering-control-plane/0.1",
        max_download_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.token = token
        self.user_agent = user_agent
        self.max_download_bytes = max_download_bytes

    @classmethod
    def from_environment(
        cls,
        token_env: str,
        *,
        max_download_bytes: int,
    ) -> "GitHubClient":
        return cls(
            token=os.environ.get(token_env),
            max_download_bytes=max_download_bytes,
        )

    def _request(self, url: str, *, accept: str = "application/vnd.github+json"):
        headers = {
            "Accept": accept,
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers, method="GET")

    def _json(self, url: str) -> Any:
        try:
            with urllib.request.urlopen(self._request(url), timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise GitHubAccessError(
                f"GitHub access failed with HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise GitHubAccessError(f"GitHub network access failed: {error.reason}") from error

    def list_repositories(self, owner: str) -> list[RepositoryDescriptor]:
        encoded = urllib.parse.quote(owner, safe="")
        if self.token:
            url = (
                f"{self.API_ROOT}/user/repos?per_page=100&affiliation=owner"
                "&sort=full_name"
            )
        else:
            url = f"{self.API_ROOT}/users/{encoded}/repos?per_page=100&sort=full_name"
        payload = self._json(url)
        repositories = []
        for item in payload:
            item_owner = str(item["owner"]["login"])
            if item_owner.lower() != owner.lower():
                continue
            repositories.append(
                RepositoryDescriptor(
                    id=int(item["id"]),
                    name=str(item["name"]),
                    full_name=str(item["full_name"]),
                    visibility=str(item.get("visibility") or "public"),
                    default_branch=str(item["default_branch"]),
                    clone_url=str(item["clone_url"]),
                    archived=bool(item.get("archived", False)),
                )
            )
        return sorted(repositories, key=lambda repo: repo.name.lower())

    def default_branch_sha(self, repository: RepositoryDescriptor) -> str:
        full_name = urllib.parse.quote(repository.full_name, safe="/")
        branch = urllib.parse.quote(repository.default_branch, safe="")
        payload = self._json(
            f"{self.API_ROOT}/repos/{full_name}/commits/{branch}"
        )
        return str(payload["sha"])

    @staticmethod
    def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        members = []
        for member in archive.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("GitHub archive contains an unsafe path")
            members.append(member)
        return members

    def download_snapshot(
        self,
        repository: RepositoryDescriptor,
        destination_root: str | Path,
    ) -> tuple[RepositoryDescriptor, Path]:
        if repository.visibility != "public" and not self.token:
            raise GitHubAccessError("private repository authentication is required")
        sha = self.default_branch_sha(repository)
        descriptor = replace(repository, commit_sha=sha)
        destination = (
            Path(destination_root)
            / repository.full_name.split("/", 1)[0]
            / repository.name
            / sha
        )
        marker = destination / ".snapshot-complete"
        if marker.is_file():
            return descriptor, destination

        full_name = urllib.parse.quote(repository.full_name, safe="/")
        url = f"{self.API_ROOT}/repos/{full_name}/zipball/{urllib.parse.quote(sha)}"
        try:
            with urllib.request.urlopen(
                self._request(url, accept="application/vnd.github+json"),
                timeout=60,
            ) as response:
                payload = response.read(self.max_download_bytes + 1)
        except urllib.error.HTTPError as error:
            raise GitHubAccessError(
                f"snapshot download failed with HTTP {error.code}"
            ) from error
        if len(payload) > self.max_download_bytes:
            raise GitHubAccessError("repository snapshot exceeds the download limit")

        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = self._safe_members(archive)
            if sum(member.file_size for member in members) > self.max_download_bytes:
                raise GitHubAccessError(
                    "repository snapshot exceeds the uncompressed size limit"
                )
            roots = {
                Path(member.filename).parts[0]
                for member in members
                if Path(member.filename).parts
            }
            if len(roots) != 1:
                raise ValueError("GitHub archive has an unexpected root layout")
            root_name = next(iter(roots))
            for member in members:
                parts = Path(member.filename).parts
                relative_parts = parts[1:] if parts and parts[0] == root_name else parts
                if not relative_parts:
                    continue
                target = destination.joinpath(*relative_parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    output.write(source.read())
        marker.write_text(sha + "\n", encoding="utf-8")
        return descriptor, destination
