from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 2


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    endpoint = "https://api.github.com/graphql"

    def __init__(self, token: str) -> None:
        self.token = token

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = None
        last_error: Exception | None = None
        retry_after: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            retry_after = None
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps({"query": query, "variables": variables}).encode(),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "User-Agent": "knowledge-graph-collector/0.1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    response_payload = json.load(response)
                if response_payload.get("data") is None and not response_payload.get("errors"):
                    last_error = GitHubError("GitHub GraphQL response did not contain data")
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(RETRY_BASE_DELAY**attempt)
                    continue
                payload = response_payload
                break
            except urllib.error.HTTPError as error:
                last_error = error
                retry_after = error.headers.get("Retry-After")
                if error.code not in RETRYABLE_STATUSES:
                    break
            except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError) as error:
                last_error = error
                retry_after = None
            if attempt < MAX_ATTEMPTS - 1:
                try:
                    delay = float(retry_after) if retry_after else RETRY_BASE_DELAY**attempt
                except ValueError:
                    delay = RETRY_BASE_DELAY**attempt
                time.sleep(delay)
        if payload is None:
            raise GitHubError(f"GitHub API request failed: {last_error}") from last_error
        if payload.get("errors"):
            messages = "; ".join(item["message"] for item in payload["errors"])
            raise GitHubError(messages)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubError("GitHub GraphQL response did not contain data")
        return data

    def list_repositories(self, owner: str) -> list[dict[str, Any]]:
        query = """
        query Repositories($owner: String!, $cursor: String) {
          repositoryOwner(login: $owner) {
            repositories(first: 50, after: $cursor, ownerAffiliations: OWNER,
                         orderBy: {field: NAME, direction: ASC}) {
              nodes {
                name nameWithOwner url sshUrl isPrivate isArchived isFork
                updatedAt pushedAt
                defaultBranchRef { name target { ... on Commit { oid } } }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        repositories: list[dict[str, Any]] = []
        cursor = None
        while True:
            data = self.query(query, {"owner": owner, "cursor": cursor})
            repository_owner = data.get("repositoryOwner")
            if repository_owner is None:
                raise GitHubError(f"GitHub owner not found: {owner}")
            connection = repository_owner["repositories"]
            repositories.extend(connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                return repositories
            cursor = connection["pageInfo"]["endCursor"]

    def collect_repository(
        self, owner: str, name: str, history_limit: int
    ) -> dict[str, Any]:
        query = """
        query RepositoryDetails($owner: String!, $name: String!, $limit: Int!) {
          repository(owner: $owner, name: $name) {
            id name nameWithOwner description url sshUrl isPrivate isArchived isFork
            createdAt updatedAt pushedAt diskUsage primaryLanguage { name }
            licenseInfo { spdxId } repositoryTopics(first: 30) { nodes { topic { name } } }
            defaultBranchRef {
              name
              target {
                ... on Commit {
                  oid
                  history(first: $limit) {
                    totalCount
                    nodes {
                      oid messageHeadline committedDate url
                      additions deletions changedFilesIfAvailable
                      author { name email user { login url } }
                      committer { name email user { login url } }
                      associatedPullRequests(first: 10) { nodes { number url } }
                    }
                  }
                }
              }
            }
            pullRequests(first: $limit, orderBy: {field: UPDATED_AT, direction: DESC}) {
              totalCount
              nodes {
                number title state url createdAt updatedAt mergedAt closedAt
                author { login url } baseRefName headRefName
                additions deletions changedFiles
                labels(first: 30) { nodes { name } }
              }
            }
            issues(first: $limit, orderBy: {field: UPDATED_AT, direction: DESC}) {
              totalCount
              nodes {
                number title state url createdAt updatedAt closedAt
                author { login url } labels(first: 30) { nodes { name } }
              }
            }
          }
        }
        """
        data = self.query(
            query, {"owner": owner, "name": name, "limit": history_limit}
        )
        repository = data.get("repository")
        if repository is None:
            raise GitHubError(f"Repository not found: {owner}/{name}")
        return normalize_repository(repository)


def normalize_repository(repository: dict[str, Any]) -> dict[str, Any]:
    branch = repository.pop("defaultBranchRef", None)
    target = (branch or {}).get("target") or {}
    history = target.get("history") or {
        "totalCount": 0,
        "nodes": [],
    }
    pull_requests = repository.pop("pullRequests")
    issues = repository.pop("issues")
    topics = repository.pop("repositoryTopics")["nodes"]
    repository["default_branch"] = (branch or {}).get("name")
    repository["head_oid"] = target.get("oid")
    repository["topics"] = [item["topic"]["name"] for item in topics]
    repository["primaryLanguage"] = (repository.get("primaryLanguage") or {}).get("name")
    repository["licenseInfo"] = (repository.get("licenseInfo") or {}).get("spdxId")

    commits = history["nodes"]
    contributors: Counter[tuple[str, str, str]] = Counter()
    for commit in commits:
        author = commit.get("author") or {}
        user = author.get("user") or {}
        contributors[(user.get("login") or "", author.get("name") or "", author.get("email") or "")] += 1
        commit["associatedPullRequests"] = commit["associatedPullRequests"]["nodes"]

    for item in pull_requests["nodes"] + issues["nodes"]:
        item["labels"] = [label["name"] for label in item["labels"]["nodes"]]

    return {
        "repository": repository,
        "commits": {"total_count": history["totalCount"], "items": commits},
        "pull_requests": {
            "total_count": pull_requests["totalCount"],
            "items": pull_requests["nodes"],
        },
        "issues": {"total_count": issues["totalCount"], "items": issues["nodes"]},
        "contributors": [
            {"login": login or None, "name": author_name, "email": email, "commits_in_sample": count}
            for (login, author_name, email), count in contributors.most_common()
        ],
    }