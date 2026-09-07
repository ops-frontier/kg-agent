from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterable

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable


SCALAR_TYPES = (str, int, float, bool)
CALL_EDGE_BATCH_SIZE = 1_000
FIXES_PATTERN = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)
CONSTRAINTS = (
    "CREATE CONSTRAINT kg_repository_full_name IF NOT EXISTS FOR (n:Repository) REQUIRE n.full_name IS UNIQUE",
    "CREATE CONSTRAINT kg_file_id IF NOT EXISTS FOR (n:File) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_function_id IF NOT EXISTS FOR (n:Function) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_class_id IF NOT EXISTS FOR (n:Class) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_commit_id IF NOT EXISTS FOR (n:Commit) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_pull_request_id IF NOT EXISTS FOR (n:PullRequest) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_issue_id IF NOT EXISTS FOR (n:Issue) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_user_id IF NOT EXISTS FOR (n:User) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_manifest_id IF NOT EXISTS FOR (n:Manifest) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_dependency_id IF NOT EXISTS FOR (n:Dependency) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT kg_collection_index_owner IF NOT EXISTS FOR (n:CollectionIndex) REQUIRE n.owner IS UNIQUE",
)


def graph_store_from_env() -> Neo4jGraphStore:
    return Neo4jGraphStore(
        uri=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=os.environ.get("NEO4J_PASSWORD", "kgpassword"),
        database=os.environ.get("NEO4J_DATABASE", "neo4j"),
        transaction_retry_seconds=float(os.environ.get("NEO4J_TRANSACTION_RETRY_SECONDS", "120")),
    )


class Neo4jGraphStore:
    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
        transaction_retry_seconds: float = 120,
    ) -> None:
        self.uri = uri
        self.database = database
        self.driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            max_transaction_retry_time=transaction_retry_seconds,
        )

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> Neo4jGraphStore:
        self.wait_until_ready()
        self.ensure_schema()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def wait_until_ready(self, attempts: int = 30, delay: float = 2.0) -> None:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                with self.driver.session(database=self.database) as session:
                    session.run("RETURN 1").consume()
                return
            except (ServiceUnavailable, Neo4jError, OSError, ValueError) as error:
                last_error = error
                time.sleep(delay)
        raise RuntimeError(f"Neo4j is not ready: {last_error}")

    def ensure_schema(self) -> None:
        with self.driver.session(database=self.database) as session:
            for query in CONSTRAINTS:
                session.run(query).consume()

    def repository_is_current(self, repository: dict[str, Any]) -> bool:
        branch = repository.get("defaultBranchRef") or {}
        target = branch.get("target") or {}
        with self.driver.session(database=self.database) as session:
            record = session.execute_read(_repository_fingerprint_tx, repository["nameWithOwner"])
        return bool(record) and all(
            record[key] == value
            for key, value in {
                "updatedAt": repository.get("updatedAt"),
                "pushedAt": repository.get("pushedAt"),
                "head_oid": target.get("oid"),
            }.items()
        )

    def existing_result(self, repository: dict[str, Any]) -> dict[str, Any]:
        with self.driver.session(database=self.database) as session:
            record = session.execute_read(_existing_result_tx, repository["nameWithOwner"])
        return {
            "repository": repository["nameWithOwner"],
            "status": "ok",
            "database": self.uri,
            "source_files": record["source_files"] if record else 0,
            "manifests": record["manifests"] if record else 0,
        }

    def write_repository(
        self,
        owner: str,
        github_data: dict[str, Any],
        source_data: list[dict[str, Any]],
        manifests: list[dict[str, Any]],
    ) -> None:
        payload = repository_payload(owner, github_data, source_data, manifests)
        with self.driver.session(database=self.database) as session:
            session.execute_write(_write_repository_tx, payload)

    def resolve_call_edges(self, owner: str) -> None:
        with self.driver.session(database=self.database) as session:
            functions = session.execute_read(_read_functions_tx, owner)
            resolutions = call_edge_resolutions(owner, functions)
            for repository, resolution in resolutions.items():
                session.execute_write(_clear_call_edges_tx, owner, repository)
                external_nodes = list(resolution["external_nodes"].values())
                for start in range(0, len(external_nodes), CALL_EDGE_BATCH_SIZE):
                    session.execute_write(
                        _write_external_functions_tx,
                        external_nodes[start:start + CALL_EDGE_BATCH_SIZE],
                    )
                edges = resolution["edges"]
                for start in range(0, len(edges), CALL_EDGE_BATCH_SIZE):
                    session.execute_write(
                        _write_call_edges_tx,
                        edges[start:start + CALL_EDGE_BATCH_SIZE],
                    )

    def write_collection_index(self, owner: str, index: dict[str, Any]) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(_write_collection_index_tx, owner, index)

    def read_collection_index(self, owner: str) -> dict[str, Any]:
        with self.driver.session(database=self.database) as session:
            record = session.run(
                "MATCH (i:CollectionIndex {owner: $owner}) RETURN i.data AS data",
                owner=owner,
            ).single()
            if record and record["data"]:
                return json.loads(record["data"])
            records = list(session.run(
                """
                MATCH (r:Repository {owner: $owner})
                RETURN r.full_name AS repository, coalesce(r.source_files, 0) AS source_files,
                       coalesce(r.manifests, 0) AS manifests
                ORDER BY toLower(r.full_name)
                """,
                owner=owner,
            ))
        return {
            "schema_version": 1,
            "owner": owner,
            "repositories": [
                {
                    "repository": record["repository"],
                    "status": "ok",
                    "database": self.uri,
                    "source_files": record["source_files"],
                    "manifests": record["manifests"],
                }
                for record in records
            ],
        }

    def repositories(self, owner: str) -> dict[str, Any]:
        with self.driver.session(database=self.database) as session:
            records = list(session.run(
                """
                MATCH (r:Repository {owner: $owner})
                RETURN r.name AS name, r.full_name AS full_name, r.description AS description,
                       r.url AS url, r.updatedAt AS updated_at, r.pushedAt AS pushed_at,
                       r.head_oid AS head_oid
                ORDER BY toLower(r.full_name)
                """,
                owner=owner,
            ))
        return {"owner": owner, "repositories": [dict(record) for record in records]}

    def repository_detail(self, owner: str, repository: str) -> dict[str, Any] | None:
        full_name = repository if "/" in repository else f"{owner}/{repository}"
        with self.driver.session(database=self.database) as session:
            record = session.run(
                """
                MATCH (r:Repository {full_name: $full_name})
                  CALL { WITH r OPTIONAL MATCH (r)-[:CONTAINS]->(item:File) RETURN count(DISTINCT item) AS files }
                  CALL { WITH r OPTIONAL MATCH (r)-[:CONTAINS]->(:File)-[:DEFINES]->(item:Function) RETURN count(DISTINCT item) AS functions }
                  CALL { WITH r OPTIONAL MATCH (r)-[:CONTAINS]->(:File)-[:DEFINES]->(item:Class) RETURN count(DISTINCT item) AS classes }
                  CALL { WITH r OPTIONAL MATCH (r)-[:HAS_MANIFEST]->(:Manifest)-[:DECLARES]->(item:Dependency) RETURN count(DISTINCT item) AS dependencies }
                CALL {
                    WITH r
                    OPTIONAL MATCH (r)-[:HAS_COMMIT]->(item:Commit)
                    RETURN count(DISTINCT item) AS commits, max(item.committedDate) AS last_commit_at
                }
                  CALL { WITH r OPTIONAL MATCH (r)-[:HAS_ISSUE]->(item:Issue) RETURN count(DISTINCT item) AS issues }
                  CALL {
                      WITH r
                      OPTIONAL MATCH (item:User)-[:AUTHORED]->(authored)
                      WHERE authored.repository = r.full_name
                      RETURN count(DISTINCT item) AS users
                  }
                RETURN r.name AS name, r.full_name AS full_name, r.description AS description,
                       r.url AS url, r.default_branch AS default_branch,
                      r.head_oid AS head_oid, last_commit_at,
                      files, functions, classes, dependencies, commits, issues, users
                """,
                full_name=full_name,
            ).single()
        if not record:
            return None
        data = dict(record)
        data["counts"] = {
            key: data.pop(key)
            for key in ("files", "functions", "classes", "dependencies", "commits", "issues", "users")
        }
        return data

    def graph_data(self, owner: str, repository: str) -> dict[str, Any]:
        full_name = repository if "/" in repository else f"{owner}/{repository}"
        with self.driver.session(database=self.database) as session:
            records = list(session.run(
                """
                MATCH (:Repository {full_name: $full_name})-[:CONTAINS]->(:File)-[:DEFINES]->(fn:Function)
                OPTIONAL MATCH (fn)-[:CALLS]->(target:Function)
                RETURN fn.id AS id, fn.name AS name, fn.file AS file, fn.start_line AS line,
                       collect(target.id) AS calls
                ORDER BY fn.file, fn.start_line, fn.name
                """,
                full_name=full_name,
            ))
        return {"nodes": [dict(record) for record in records]}


def repository_payload(
    owner: str,
    github_data: dict[str, Any],
    source_data: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
) -> dict[str, Any]:
    repository = github_data["repository"]
    full_name = repository["nameWithOwner"]
    repository_properties = clean_properties(repository)
    repository_properties.update({
        "owner": owner,
        "repository": full_name,
        "full_name": full_name,
        "source_files": len(source_data),
        "functions": sum(len(item["functions"]) for item in source_data),
        "classes": sum(len(item["classes"]) for item in source_data),
        "manifests": len(manifests),
        "dependencies": sum(len(item["dependencies"]) for item in manifests),
    })
    return {
        "repository": repository_properties,
        "files": file_payloads(full_name, source_data),
        "functions": function_payloads(full_name, owner, source_data),
        "classes": class_payloads(full_name, owner, source_data),
        "commits": commit_payloads(full_name, owner, github_data["commits"]["items"]),
        "pull_requests": pull_request_payloads(full_name, owner, github_data["pull_requests"]["items"]),
        "issues": issue_payloads(full_name, owner, github_data["issues"]["items"]),
        "contributors": user_payloads(github_data["contributors"]),
        "manifests": manifest_payloads(full_name, owner, manifests),
        "dependencies": dependency_payloads(full_name, owner, manifests),
        "repository_dependencies": repository_dependency_payloads(full_name, owner, manifests),
        "fixes": fixes_payloads(full_name, github_data["commits"]["items"]),
    }


def file_payloads(repository: str, source_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{repository}:{item['path']}",
            "repository": repository,
            "path": item["path"],
            "language": item.get("language"),
            "parse_has_error": item.get("parse_has_error", False),
            "parse_error": item.get("parse_error"),
            "imports": item.get("imports", []),
        }
        for item in source_data
    ]


def function_payloads(repository: str, owner: str, source_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in source_data:
        for function in item["functions"]:
            result.append({
                **clean_properties(function),
                "id": symbol_id(repository, item["path"], function),
                "owner": owner,
                "repository": repository,
                "file": item["path"],
                "calls": function.get("calls", []),
            })
    return result


def class_payloads(repository: str, owner: str, source_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in source_data:
        for class_item in item["classes"]:
            result.append({
                **clean_properties(class_item),
                "id": symbol_id(repository, item["path"], class_item),
                "owner": owner,
                "repository": repository,
                "file": item["path"],
            })
    return result


def commit_payloads(repository: str, owner: str, commits: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for commit in commits:
        author = commit.get("author") or {}
        user = author.get("user") or {}
        result.append({
            **clean_properties(commit, exclude={"author", "committer", "associatedPullRequests"}),
            "id": f"{repository}:commit:{commit['oid']}",
            "owner": owner,
            "repository": repository,
            "author_user_id": user_id(user.get("login"), author.get("email"), author.get("name")),
            "author_login": user.get("login"),
            "author_name": author.get("name"),
            "author_email": author.get("email"),
            "pull_request_numbers": [item["number"] for item in commit.get("associatedPullRequests", [])],
        })
    return result


def pull_request_payloads(repository: str, owner: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **clean_properties(item, exclude={"author"}),
            "id": f"{repository}:pull_request:{item['number']}",
            "owner": owner,
            "repository": repository,
            "author_user_id": user_id((item.get("author") or {}).get("login"), None, None),
            "author_login": (item.get("author") or {}).get("login"),
        }
        for item in items
    ]


def issue_payloads(repository: str, owner: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **clean_properties(item, exclude={"author"}),
            "id": f"{repository}:issue:{item['number']}",
            "owner": owner,
            "repository": repository,
            "author_user_id": user_id((item.get("author") or {}).get("login"), None, None),
            "author_login": (item.get("author") or {}).get("login"),
        }
        for item in items
    ]


def user_payloads(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **clean_properties(item),
            "id": user_id(item.get("login"), item.get("email"), item.get("name")),
        }
        for item in items
    ]


def manifest_payloads(repository: str, owner: str, manifests: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{repository}:manifest:{manifest['path']}",
            "owner": owner,
            "repository": repository,
            "path": manifest["path"],
            "type": manifest["type"],
            "error": manifest.get("error"),
        }
        for manifest in manifests
    ]


def dependency_payloads(repository: str, owner: str, manifests: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for manifest in manifests:
        manifest_id = f"{repository}:manifest:{manifest['path']}"
        for dependency in manifest["dependencies"]:
            result.append({
                **clean_properties(dependency),
                "id": f"{manifest_id}:dependency:{dependency['scope']}:{dependency['name']}",
                "owner": owner,
                "repository": repository,
                "manifest_id": manifest_id,
            })
    return result


def repository_dependency_payloads(repository: str, owner: str, manifests: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    for manifest in manifests:
        for dependency in manifest["dependencies"]:
            if dependency.get("repository_dependency"):
                result.append({
                    "source": repository,
                    "target": f"{owner}/{dependency['repository_dependency']}",
                })
    return result


def fixes_payloads(repository: str, commits: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    for commit in commits:
        for issue_number in FIXES_PATTERN.findall(commit.get("messageHeadline") or ""):
            result.append({
                "commit_id": f"{repository}:commit:{commit['oid']}",
                "issue_id": f"{repository}:issue:{issue_number}",
            })
    return result


def symbol_id(repository: str, path: str, symbol: dict[str, Any]) -> str:
    return f"{repository}:{path}:{symbol.get('start_line', 0)}:{symbol.get('name', '<anonymous>')}"


def user_id(login: str | None, email: str | None, name: str | None) -> str:
    return login or email or name or "unknown"


def clean_properties(data: dict[str, Any], exclude: set[str] | None = None) -> dict[str, Any]:
    result = {}
    excluded = exclude or set()
    for key, value in data.items():
        if key in excluded or value is None:
            continue
        if isinstance(value, SCALAR_TYPES):
            result[key] = value
        elif isinstance(value, list) and all(isinstance(item, SCALAR_TYPES) for item in value):
            result[key] = value
        else:
            result[f"{key}_json"] = json.dumps(value, ensure_ascii=False)
    return result


def _repository_fingerprint_tx(tx: Any, full_name: str) -> Any:
    return tx.run(
        """
        MATCH (r:Repository {full_name: $full_name})
        RETURN r.updatedAt AS updatedAt, r.pushedAt AS pushedAt, r.head_oid AS head_oid
        """,
        full_name=full_name,
    ).single()


def _existing_result_tx(tx: Any, full_name: str) -> Any:
    return tx.run(
        """
        MATCH (r:Repository {full_name: $full_name})
        OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
        OPTIONAL MATCH (r)-[:HAS_MANIFEST]->(m:Manifest)
        RETURN count(DISTINCT f) AS source_files, count(DISTINCT m) AS manifests
        """,
        full_name=full_name,
    ).single()


def _read_functions_tx(tx: Any, owner: str) -> list[dict[str, Any]]:
    records = tx.run(
        """
        MATCH (fn:Function {owner: $owner})
        RETURN fn.id AS id, fn.repository AS repository, fn.file AS file,
               fn.name AS name, fn.calls AS calls, coalesce(fn.external, false) AS external
        """,
        owner=owner,
    )
    return [dict(record) for record in records]


def _write_collection_index_tx(tx: Any, owner: str, index: dict[str, Any]) -> None:
    tx.run(
        """
        MERGE (i:CollectionIndex {owner: $owner})
        SET i.generated_at = $generated_at, i.data = $data
        """,
        owner=owner,
        generated_at=index["generated_at"],
        data=json.dumps(index, ensure_ascii=False),
    ).consume()


def _write_repository_tx(tx: Any, payload: dict[str, Any]) -> None:
    repository = payload["repository"]
    tx.run(
        """
        MATCH (n {repository: $repository})
        WHERE NOT n:Repository
        DETACH DELETE n
        """,
        repository=repository["full_name"],
    ).consume()
    tx.run(
        "MATCH (:Repository {full_name: $repository})-[r:DEPENDS_ON]->() DELETE r",
        repository=repository["full_name"],
    ).consume()
    tx.run(
        """
        MERGE (r:Repository {full_name: $repository.full_name})
        SET r += $repository
        """,
        repository=repository,
    ).consume()
    tx.run(
        """
        MATCH (r:Repository {full_name: $repository})
        UNWIND $files AS item
        MERGE (f:File {id: item.id})
        SET f += item
        MERGE (r)-[:CONTAINS]->(f)
        """,
        repository=repository["full_name"],
        files=payload["files"],
    ).consume()
    tx.run(
        """
        UNWIND $functions AS item
        MATCH (f:File {id: item.repository + ':' + item.file})
        MERGE (fn:Function {id: item.id})
        SET fn += item, fn.external = false
        MERGE (f)-[:DEFINES]->(fn)
        """,
        functions=payload["functions"],
    ).consume()
    tx.run(
        """
        UNWIND $classes AS item
        MATCH (f:File {id: item.repository + ':' + item.file})
        MERGE (c:Class {id: item.id})
        SET c += item
        MERGE (f)-[:DEFINES]->(c)
        """,
        classes=payload["classes"],
    ).consume()
    tx.run(
        """
        MATCH (r:Repository {full_name: $repository})
        UNWIND $commits AS item
        MERGE (c:Commit {id: item.id})
        SET c += item
        MERGE (r)-[:HAS_COMMIT]->(c)
        WITH c, item WHERE item.author_user_id <> 'unknown'
        MERGE (u:User {id: item.author_user_id})
        SET u.login = item.author_login, u.name = item.author_name, u.email = item.author_email
        MERGE (u)-[:AUTHORED]->(c)
        """,
        repository=repository["full_name"],
        commits=payload["commits"],
    ).consume()
    tx.run(
        """
        MATCH (r:Repository {full_name: $repository})
        UNWIND $pull_requests AS item
        MERGE (pr:PullRequest {id: item.id})
        SET pr += item
        MERGE (r)-[:HAS_PULL_REQUEST]->(pr)
        WITH pr, item WHERE item.author_user_id <> 'unknown'
        MERGE (u:User {id: item.author_user_id})
        SET u.login = item.author_login
        MERGE (u)-[:AUTHORED]->(pr)
        """,
        repository=repository["full_name"],
        pull_requests=payload["pull_requests"],
    ).consume()
    tx.run(
        """
        MATCH (r:Repository {full_name: $repository})
        UNWIND $issues AS item
        MERGE (i:Issue {id: item.id})
        SET i += item
        MERGE (r)-[:HAS_ISSUE]->(i)
        WITH i, item WHERE item.author_user_id <> 'unknown'
        MERGE (u:User {id: item.author_user_id})
        SET u.login = item.author_login
        MERGE (u)-[:AUTHORED]->(i)
        """,
        repository=repository["full_name"],
        issues=payload["issues"],
    ).consume()
    tx.run(
        """
        UNWIND $contributors AS item
        MERGE (u:User {id: item.id})
        SET u += item
        """,
        contributors=payload["contributors"],
    ).consume()
    tx.run(
        """
        MATCH (r:Repository {full_name: $repository})
        UNWIND $manifests AS item
        MERGE (m:Manifest {id: item.id})
        SET m += item
        MERGE (r)-[:HAS_MANIFEST]->(m)
        """,
        repository=repository["full_name"],
        manifests=payload["manifests"],
    ).consume()
    tx.run(
        """
        UNWIND $dependencies AS item
        MATCH (m:Manifest {id: item.manifest_id})
        MERGE (d:Dependency {id: item.id})
        SET d += item
        MERGE (m)-[:DECLARES]->(d)
        """,
        dependencies=payload["dependencies"],
    ).consume()
    tx.run(
        """
        UNWIND $repository_dependencies AS item
        MATCH (source:Repository {full_name: item.source})
        MERGE (target:Repository {full_name: item.target})
        SET target.owner = $owner, target.repository = item.target
        MERGE (source)-[:DEPENDS_ON]->(target)
        """,
        owner=repository["owner"],
        repository_dependencies=payload["repository_dependencies"],
    ).consume()
    tx.run(
        """
        UNWIND $fixes AS item
        MATCH (c:Commit {id: item.commit_id})
        MATCH (i:Issue {id: item.issue_id})
        MERGE (c)-[:FIXES]->(i)
        """,
        fixes=payload["fixes"],
    ).consume()


def call_edge_resolutions(
    owner: str,
    functions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    internal = [function for function in functions if not function["external"]]
    by_name: dict[str, list[str]] = {}
    by_file_and_name: dict[tuple[str, str, str], list[str]] = {}
    for function in internal:
        by_name.setdefault(function["name"], []).append(function["id"])
        by_file_and_name.setdefault((function["repository"], function["file"], function["name"]), []).append(function["id"])
    result: dict[str, dict[str, Any]] = {}
    for function in internal:
        resolution = result.setdefault(
            function["repository"],
            {"external_nodes": {}, "edges": []},
        )
        for called_name in function.get("calls") or []:
            local_targets = by_file_and_name.get((function["repository"], function["file"], called_name), [])
            global_targets = by_name.get(called_name, [])
            if local_targets:
                target_ids = local_targets
            elif len(global_targets) == 1:
                target_ids = global_targets
            else:
                external_id = f"{function['repository']}::external::{called_name}"
                resolution["external_nodes"][external_id] = {
                    "id": external_id,
                    "owner": owner,
                    "repository": function["repository"],
                    "name": called_name,
                    "external": True,
                }
                target_ids = [external_id]
            resolution["edges"].extend(
                {"source": function["id"], "target": target_id}
                for target_id in target_ids
            )
    return result


def _clear_call_edges_tx(tx: Any, owner: str, repository: str) -> None:
    tx.run(
        "MATCH (fn:Function {owner: $owner, repository: $repository})-[r:CALLS]->() DELETE r",
        owner=owner,
        repository=repository,
    ).consume()
    tx.run(
        "MATCH (fn:Function {owner: $owner, repository: $repository, external: true}) DETACH DELETE fn",
        owner=owner,
        repository=repository,
    ).consume()


def _write_external_functions_tx(tx: Any, external_nodes: list[dict[str, Any]]) -> None:
    tx.run(
        """
        UNWIND $external_nodes AS item
        MERGE (fn:Function {id: item.id})
        SET fn += item
        """,
        external_nodes=external_nodes,
    ).consume()


def _write_call_edges_tx(tx: Any, edges: list[dict[str, str]]) -> None:
    tx.run(
        """
        UNWIND $edges AS item
        MATCH (source:Function {id: item.source})
        MATCH (target:Function {id: item.target})
        MERGE (source)-[:CALLS]->(target)
        """,
        edges=edges,
    ).consume()