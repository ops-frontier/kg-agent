from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .dependencies import analyze_manifests
from .github import GitHubClient, GitHubError
from .source import analyze_file_isolated, iter_source_files
from .storage import Neo4jGraphStore, graph_store_from_env


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Collect repository knowledge into Neo4j")
    result.add_argument("--owner", default=os.environ.get("GH_TARGET_ORGANIZATION"))
    result.add_argument("--repository", action="append", dest="repositories")
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

    with graph_store_from_env() as store:
        repository_names = {repository["name"] for repository in repositories}
        results = []
        repositories_to_collect = []
        for repository in repositories:
            if repository_is_current(store, repository):
                result = store.existing_result(repository)
                print(f"unchanged {repository['nameWithOwner']}")
                results.append(result)
            else:
                repositories_to_collect.append(repository)
        if progress:
            progress("準備中", f"{len(repositories_to_collect)} リポジトリを解析します")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(collect_one, client, store, args, repository, repository_names): repository
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
        if repositories_to_collect:
            store.resolve_call_edges(args.owner)
        if selected:
            previous_results = {
                item["repository"]: item
                for item in store.read_collection_index(args.owner).get("repositories", [])
            }
            previous_results.update({item["repository"]: item for item in results})
            results = list(previous_results.values())
        index = {
            "schema_version": 1,
            "owner": args.owner,
            "generated_at": datetime.now(UTC).isoformat(),
            "repositories": sorted(results, key=lambda item: item["repository"].lower()),
        }
        store.write_collection_index(args.owner, index)
        run_failures = sum(item["status"] == "failed" for item in results if not selected or item["repository"].split("/", 1)[-1] in selected)
        total_failures = sum(item["status"] == "failed" for item in results)
        print(f"finished: {len(results) - total_failures} succeeded, {total_failures} failed")
        return 1 if run_failures else 0


def repository_is_current(store: Neo4jGraphStore, repository: dict[str, Any]) -> bool:
    return store.repository_is_current(repository)


def collect_one(
    client: GitHubClient,
    store: Neo4jGraphStore,
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
    store.write_repository(args.owner, github_data, source_data, manifests)
    return {
        "repository": repository["nameWithOwner"],
        "status": "ok",
        "database": store.uri,
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