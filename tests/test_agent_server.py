import http.client
import json
import logging
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kg_agent import server as agent_server


def start_server(directory):
    handler = partial(agent_server.Handler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class Neo4jStubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_chat_returns_graphrag_response(monkeypatch, tmp_path) -> None:
    class StubAgent:
        def answer(self, question, progress, history):
            assert question == "影響範囲は？"
            assert history == [{"role": "user", "text": "delivery-api を調べて"}]
            progress({"stage": "search", "iteration": 1})
            return {"message": "呼び出し元が影響を受けます", "sources": [{"function": "caller"}]}

    monkeypatch.setattr(agent_server, "get_agent", lambda: StubAgent())
    server = start_server(tmp_path)
    connection = http.client.HTTPConnection(*server.server_address)
    try:
        body = json.dumps({
            "message": "影響範囲は？",
            "history": [{"role": "user", "text": "delivery-api を調べて"}],
        })
        connection.request("POST", "/api/chat", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "application/x-ndjson; charset=utf-8"
        assert [json.loads(line) for line in response.readlines()] == [{
            "type": "progress",
            "stage": "search",
            "iteration": 1,
        }, {
            "type": "result",
            "message": "呼び出し元が影響を受けます",
            "sources": [{"function": "caller"}],
        }]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_chat_logs_graphrag_failures(monkeypatch, tmp_path, caplog) -> None:
    class StubAgent:
        def answer(self, question, progress, history):
            progress({"stage": "search", "iteration": 1})
            raise RuntimeError("Neo4j query timed out")

    monkeypatch.setattr(agent_server, "get_agent", lambda: StubAgent())
    caplog.set_level(logging.INFO, logger="kg_agent.server")
    server = start_server(tmp_path)
    connection = http.client.HTTPConnection(*server.server_address)
    try:
        body = json.dumps({"message": "changeStream.ts の影響範囲は？"})
        connection.request("POST", "/api/chat", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        events = [json.loads(line) for line in response.readlines()]

        assert response.status == 200
        assert events[-1] == {"type": "error", "error": "Neo4j query timed out"}
        assert "chat request failed" in caplog.text
        assert "Neo4j query timed out" in caplog.text
        assert "chat progress" in caplog.text
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_proxy_treats_broken_pipe_as_client_disconnect(monkeypatch) -> None:
    handler = object.__new__(agent_server.Handler)
    handler.close_connection = False

    def disconnected(self, base_url, path):
        raise BrokenPipeError

    monkeypatch.setattr(agent_server.Handler, "_proxy", disconnected)

    handler.proxy()

    assert handler.close_connection


def test_spa_falls_back_to_index_for_browser_routes(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<main>kg-agent</main>")
    server = start_server(tmp_path)
    connection = http.client.HTTPConnection(*server.server_address)
    try:
        connection.request("GET", "/repositories/owner%2Frepository")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"<main>kg-agent</main>"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_neo4j_browser_path_is_proxied(monkeypatch, tmp_path) -> None:
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Neo4jStubHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    monkeypatch.setattr(
        agent_server,
        "NEO4J_URL",
        f"http://{upstream.server_address[0]}:{upstream.server_address[1]}",
    )
    server = start_server(tmp_path)
    connection = http.client.HTTPConnection(*server.server_address)
    try:
        connection.request("GET", "/neo4j/browser/?sample=true")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"/browser/?sample=true"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()