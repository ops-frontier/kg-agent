from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_repository(
    root: Path,
    github_data: dict[str, Any],
    source_data: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_yaml(root / "repository.yaml", {"schema_version": 1, **github_data["repository"]})
    for key in ("commits", "pull_requests", "issues", "contributors"):
        write_yaml(root / "github" / f"{key}.yaml", {"schema_version": 1, key: github_data[key]})
    for item in source_data:
        write_yaml(root / "code" / f"{item['path']}.yaml", item)
    write_yaml(root / "dependencies" / "manifests.yaml", {"schema_version": 1, "manifests": manifests})
    write_summary(root, github_data, source_data, manifests)


def write_summary(
    root: Path,
    github_data: dict[str, Any],
    source_data: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
) -> None:
    repository = github_data["repository"]
    function_count = sum(len(item["functions"]) for item in source_data)
    class_count = sum(len(item["classes"]) for item in source_data)
    dependency_count = sum(len(item["dependencies"]) for item in manifests)
    text = f"""# {repository['nameWithOwner']}

- Generated: {datetime.now(UTC).isoformat()}
- Default branch: {repository.get('default_branch') or 'none'}
- Source files analyzed: {len(source_data)}
- Functions: {function_count}
- Classes/interfaces: {class_count}
- Dependencies: {dependency_count}
- Commits stored: {len(github_data['commits']['items'])} / {github_data['commits']['total_count']}
- Pull requests stored: {len(github_data['pull_requests']['items'])} / {github_data['pull_requests']['total_count']}
- Issues stored: {len(github_data['issues']['items'])} / {github_data['issues']['total_count']}
"""
    (root / "SUMMARY.md").write_text(text, encoding="utf-8")