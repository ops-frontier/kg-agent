from pathlib import Path

import yaml

from kg_collector.cli import repository_is_current
from kg_collector.storage import write_repository, write_yaml


def test_write_repository_uses_source_directory_hierarchy(tmp_path: Path) -> None:
    github_data = {
        "repository": {"name": "demo", "nameWithOwner": "owner/demo", "default_branch": "main"},
        "commits": {"total_count": 1, "items": [{}]},
        "pull_requests": {"total_count": 0, "items": []},
        "issues": {"total_count": 0, "items": []},
        "contributors": [],
    }
    source_data = [{
        "schema_version": 1, "path": "src/api/app.py", "language": "python",
        "parse_has_error": False, "classes": [], "functions": [], "variables": [], "imports": [],
    }]

    write_repository(tmp_path, github_data, source_data, [])

    code_file = tmp_path / "code" / "src" / "api" / "app.py.yaml"
    assert yaml.safe_load(code_file.read_text(encoding="utf-8"))["path"] == "src/api/app.py"
    assert (tmp_path / "github" / "commits.yaml").exists()
    assert (tmp_path / "dependencies" / "manifests.yaml").exists()
    assert (tmp_path / "SUMMARY.md").exists()


def test_repository_is_current_compares_repository_fingerprint(tmp_path: Path) -> None:
    write_yaml(tmp_path / "repository.yaml", {
        "schema_version": 1,
        "updatedAt": "2026-09-01T00:00:00Z",
        "pushedAt": "2026-08-31T00:00:00Z",
        "head_oid": "abc123",
    })
    repository = {
        "updatedAt": "2026-09-01T00:00:00Z",
        "pushedAt": "2026-08-31T00:00:00Z",
        "defaultBranchRef": {"target": {"oid": "abc123"}},
    }

    assert repository_is_current(tmp_path, repository)
    repository["defaultBranchRef"]["target"]["oid"] = "def456"
    assert not repository_is_current(tmp_path, repository)