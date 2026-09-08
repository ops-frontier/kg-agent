import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from neo4j.exceptions import ServiceUnavailable

from kg_collector import web


def test_status_stream_sends_initial_and_updated_state() -> None:
    original_state = web.collection_state.copy()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address)

    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()

        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        initial = json.loads(response.readline().removeprefix(b"data: "))
        assert initial["message"] == web.collection_state["message"]
        assert response.readline() == b"\n"

        web.update_collection_state(message="テスト更新")
        updated = json.loads(response.readline().removeprefix(b"data: "))
        assert updated["message"] == "テスト更新"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        web.update_collection_state(**original_state)


def test_graph_data_reads_from_neo4j_store(monkeypatch) -> None:
    class Store:
        def graph_data(self, owner, repository):
            return {"nodes": [{"id": f"{owner}/{repository}:run", "name": "run", "calls": []}]}

    monkeypatch.setattr(web, "OWNER", "owner")
    monkeypatch.setattr(web, "graph_store", lambda: Store())

    assert web.graph_data("demo") == {"nodes": [{"id": "owner/demo:run", "name": "run", "calls": []}]}


def test_root_does_not_serve_a_web_ui() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address)

    try:
        connection.request("GET", "/")
        assert connection.getresponse().status == 404
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_repositories_response_includes_repository_glob(monkeypatch) -> None:
    class Store:
        def repositories(self, owner):
            return {
                "owner": owner,
                "repositories": [
                    {"name": "delivery-api"},
                    {"name": "inventory-api"},
                ],
            }

    monkeypatch.setattr(web, "OWNER", "owner")
    monkeypatch.setattr(web, "REPOSITORY_GLOB", "delivery-*")
    monkeypatch.setattr(web, "graph_store", lambda: Store())
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address)

    try:
        connection.request("GET", "/api/repositories")
        response = connection.getresponse()
        assert response.status == 200
        data = json.load(response)
        assert data["repository_glob"] == "delivery-*"
        assert data["repositories"] == [{"name": "delivery-api"}]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_database_failure_returns_service_unavailable(monkeypatch) -> None:
    class Store:
        def repositories(self, owner):
            raise ServiceUnavailable("database unavailable")

    monkeypatch.setattr(web, "graph_store", lambda: Store())
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address)

    try:
        connection.request("GET", "/api/repositories")
        response = connection.getresponse()
        assert response.status == 503
        assert json.load(response)["error"] == "Neo4j is unavailable"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()