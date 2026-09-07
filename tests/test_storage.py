from kg_collector.cli import repository_is_current
from kg_collector.storage import (
    Neo4jGraphStore,
    call_edge_resolutions,
    clean_properties,
    repository_payload,
)
from kg_collector.source import SOURCE_SCHEMA_VERSION


def test_repository_payload_models_files_symbols_and_dependencies() -> None:
    github_data = {
        "repository": {"name": "demo", "nameWithOwner": "owner/demo", "default_branch": "main"},
        "commits": {"total_count": 1, "items": [{"oid": "abc", "messageHeadline": "fixes #7", "author": {"name": "Ada", "email": "ada@example.com", "user": {"login": "ada"}}, "associatedPullRequests": []}]},
        "pull_requests": {"total_count": 0, "items": []},
        "issues": {"total_count": 1, "items": [{"number": 7, "title": "Bug", "author": {"login": "ada"}, "labels": []}]},
        "contributors": [],
    }
    source_data = [{
        "schema_version": 1, "path": "src/api/app.py", "language": "python",
        "parse_has_error": False, "classes": [],
        "functions": [{"name": "run", "kind": "function_definition", "start_line": 3, "end_line": 4, "calls": ["send"]}],
        "variables": [], "imports": ["from client import send"],
    }]
    manifests = [{"path": "pyproject.toml", "type": "pyproject.toml", "dependencies": [{"name": "lib", "version": "*", "scope": "dependencies", "repository_dependency": "lib"}]}]

    payload = repository_payload("owner", github_data, source_data, manifests)

    assert payload["repository"]["full_name"] == "owner/demo"
    assert payload["repository"]["source_schema_version"] == SOURCE_SCHEMA_VERSION
    assert payload["files"][0]["id"] == "owner/demo:src/api/app.py"
    assert payload["functions"][0]["calls"] == ["send"]
    assert payload["repository_dependencies"] == [{"source": "owner/demo", "target": "owner/lib"}]
    assert payload["fixes"] == [{"commit_id": "owner/demo:commit:abc", "issue_id": "owner/demo:issue:7"}]


def test_repository_is_current_uses_store_fingerprint() -> None:
    class Store:
        def __init__(self) -> None:
            self.current = True

        def repository_is_current(self, repository):
            return self.current

    store = Store()
    repository = {
        "updatedAt": "2026-09-01T00:00:00Z",
        "pushedAt": "2026-08-31T00:00:00Z",
        "defaultBranchRef": {"target": {"oid": "abc123"}},
    }

    assert repository_is_current(store, repository)
    store.current = False
    assert not repository_is_current(store, repository)


def test_clean_properties_serializes_nested_values() -> None:
    assert clean_properties({"name": "demo", "nested": {"x": 1}, "empty": None}) == {
        "name": "demo",
        "nested_json": '{"x": 1}',
    }


def test_graph_store_configures_transaction_retry(monkeypatch) -> None:
    captured = {}

    class Driver:
        def close(self):
            pass

    def create_driver(uri, **options):
        captured.update({"uri": uri, **options})
        return Driver()

    monkeypatch.setattr("kg_collector.storage.GraphDatabase.driver", create_driver)

    store = Neo4jGraphStore(
        "bolt://neo4j:7687",
        "neo4j",
        "password",
        transaction_retry_seconds=120,
    )
    store.close()

    assert captured["max_transaction_retry_time"] == 120


def test_call_edges_are_grouped_by_source_repository() -> None:
    functions = [
        {"id": "owner/a:a.py:1:run", "owner": "owner", "repository": "owner/a", "file": "a.py", "name": "run", "calls": ["shared", "missing"], "references": ["shared"], "external": False},
        {"id": "owner/b:b.py:1:shared", "owner": "owner", "repository": "owner/b", "file": "b.py", "name": "shared", "calls": [], "references": [], "external": False},
    ]

    resolutions = call_edge_resolutions("owner", functions)

    assert set(resolutions) == {"owner/a", "owner/b"}
    assert resolutions["owner/a"]["edges"] == [
        {"source": "owner/a:a.py:1:run", "target": "owner/b:b.py:1:shared"},
        {"source": "owner/a:a.py:1:run", "target": "owner/a::external::missing"},
    ]
    assert set(resolutions["owner/a"]["external_nodes"]) == {"owner/a::external::missing"}
    assert resolutions["owner/a"]["reference_edges"] == [
        {"source": "owner/a:a.py:1:run", "target": "owner/b:b.py:1:shared"},
    ]