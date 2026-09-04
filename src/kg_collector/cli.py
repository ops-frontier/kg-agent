from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from .dependencies import analyze_manifests
from .github import GitHubClient, GitHubError
from .source import analyze_file_isolated, iter_source_files
from .storage import write_repository, write_yaml


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Collect repository knowledge as YAML")
    result.add_argument("--owner", default=os.environ.get("GH_TARGET_ORGANIZATION"))
    result.add_argument("--repository", action="append", dest="repositories")
    result.add_argument(
        "--output", type=Path, default=Path(os.environ.get("KG_OUTPUT_DIR", "knowledge"))
    )
    result.add_argument("--history-limit", type=int, default=100)
    result.add_argument("--workers", type=int, default=4)
    result.add_argument("--max-file-bytes", type=int, default=1_000_000)
    result.add_argument("--include-archived", action="store_true")
    return result


def main(
    argv: list[str] | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> int:
    args = parser().parse_args(argv)
    token = os.environ.get("GH_PAT")
    if not token:
        print("GH_PAT is required", file=sys.stderr)
        return 2
    if not args.owner:
        print("GH_TARGET_ORGANIZATION is required", file=sys.stderr)
        return 2
    if not 1 <= args.history_limit <= 100:
        print("--history-limit must be between 1 and 100", file=sys.stderr)
        return 2

    client = GitHubClient(token)
    try:
        repositories = client.list_repositories(args.owner)
    except GitHubError as error:
        print(f"Repository discovery failed: {error}", file=sys.stderr)
        return 1
    selected = set(args.repositories or [])
    repositories = [
        repository for repository in repositories
        if (not selected or repository["name"] in selected)
        and (args.include_archived or not repository["isArchived"])
    ]
    if selected - {repository["name"] for repository in repositories}:
        missing = ", ".join(sorted(selected - {repository["name"] for repository in repositories}))
        print(f"Repositories not found or excluded: {missing}", file=sys.stderr)
        return 1

    repository_names = {repository["name"] for repository in repositories}
    results = []
    repositories_to_collect = []
    for repository in repositories:
        output = args.output / args.owner / repository["name"]
        if repository_is_current(output, repository):
            result = existing_result(output, repository)
            print(f"unchanged {repository['nameWithOwner']}")
            results.append(result)
        else:
            repositories_to_collect.append(repository)
    if progress:
        progress("準備中", f"{len(repositories_to_collect)} リポジトリを解析します")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(collect_one, client, args, repository, repository_names): repository
            for repository in repositories_to_collect
        }
        for future in as_completed(futures):
            repository = futures[future]
            try:
                if progress:
                    progress("解析中", repository["nameWithOwner"])
                result = future.result()
                print(f"collected {repository['nameWithOwner']}: {result['source_files']} source files")
                if progress:
                    progress("完了", repository["nameWithOwner"])
                results.append(result)
            except Exception as error:
                print(f"failed {repository['nameWithOwner']}: {error}", file=sys.stderr)
                if progress:
                    progress("失敗", f"{repository['nameWithOwner']}: {error}")
                results.append({"repository": repository["nameWithOwner"], "status": "failed", "error": str(error)})

    index_path = args.output / args.owner / "index.yaml"
    if selected and index_path.exists():
        existing_index = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
        previous_results = {
            item["repository"]: item
            for item in existing_index.get("repositories", [])
        }
        previous_results.update({item["repository"]: item for item in results})
        results = list(previous_results.values())
    index = {
        "schema_version": 1,
        "owner": args.owner,
        "generated_at": datetime.now(UTC).isoformat(),
        "repositories": sorted(results, key=lambda item: item["repository"].lower()),
    }
    write_yaml(index_path, index)
    run_failures = sum(item["status"] == "failed" for item in results if not selected or item["repository"].split("/", 1)[-1] in selected)
    total_failures = sum(item["status"] == "failed" for item in results)
    print(f"finished: {len(results) - total_failures} succeeded, {total_failures} failed")
    return 1 if run_failures else 0


def repository_is_current(output: Path, repository: dict[str, Any]) -> bool:
    metadata_path = output / "repository.yaml"
    if not metadata_path.exists():
        return False
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    branch = repository.get("defaultBranchRef") or {}
    target = branch.get("target") or {}
    return all(
        metadata.get(key) == value
        for key, value in {
            "updatedAt": repository.get("updatedAt"),
            "pushedAt": repository.get("pushedAt"),
            "head_oid": target.get("oid"),
        }.items()
    )


def existing_result(output: Path, repository: dict[str, Any]) -> dict[str, Any]:
    source_files = sum(1 for _ in (output / "code").rglob("*.yaml"))
    manifests_path = output / "dependencies" / "manifests.yaml"
    manifests_data = yaml.safe_load(manifests_path.read_text(encoding="utf-8")) or {}
    return {
        "repository": repository["nameWithOwner"],
        "status": "ok",
        "path": str(output),
        "source_files": source_files,
        "manifests": len(manifests_data.get("manifests", [])),
    }


def collect_one(
    client: GitHubClient,
    args: argparse.Namespace,
    repository: dict[str, Any],
    repository_names: set[str],
) -> dict[str, Any]:
    name = repository["name"]
    github_data = client.collect_repository(args.owner, name, args.history_limit)
    with tempfile.TemporaryDirectory(prefix=f"kg-{name}-") as temporary:
        checkout = Path(temporary) / name
        clone(repository["nameWithOwner"], checkout)
        source_data = [
            analyze_file_isolated(path, checkout, language)
            for path, language in iter_source_files(checkout, args.max_file_bytes)
        ]
        manifests = analyze_manifests(checkout, repository_names)
    output = args.output / args.owner / name
    if output.exists():
        shutil.rmtree(output)
    write_repository(output, github_data, source_data, manifests)
    return {
        "repository": repository["nameWithOwner"],
        "status": "ok",
        "path": str(output),
        "source_files": len(source_data),
        "manifests": len(manifests),
    }


def clone(name_with_owner: str, destination: Path) -> None:
    environment = os.environ.copy()
    environment["GH_TOKEN"] = environment["GH_PAT"]
    process = subprocess.run(
        ["gh", "repo", "clone", name_with_owner, str(destination), "--", "--depth=1"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "git clone failed")


if __name__ == "__main__":
    raise SystemExit(main())