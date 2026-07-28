from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import RepositoryDescriptor


def load_inventory(path: str | Path) -> tuple[dict[str, Any], list[RepositoryDescriptor]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("inventory root must be an object")
    repositories = payload.get("repositories")
    if not isinstance(repositories, list):
        raise ValueError("inventory repositories must be a list")
    descriptors = [RepositoryDescriptor.from_dict(item) for item in repositories]
    full_names = [repository.full_name.lower() for repository in descriptors]
    if len(full_names) != len(set(full_names)):
        raise ValueError("inventory contains duplicate repositories")
    return payload, sorted(descriptors, key=lambda item: item.name.lower())


def inventory_payload(
    owner: str,
    repositories: list[RepositoryDescriptor],
    *,
    source: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "owner": owner,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "repositories": [repository.to_dict() for repository in repositories],
    }


def merge_inventory(
    baseline: list[RepositoryDescriptor],
    live: list[RepositoryDescriptor],
) -> list[RepositoryDescriptor]:
    """Keep authenticated/private baseline entries when an anonymous API omits them."""
    merged = {item.full_name.lower(): item for item in baseline}
    merged.update({item.full_name.lower(): item for item in live})
    return sorted(merged.values(), key=lambda item: item.name.lower())


def write_inventory(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
