import http.client
import json
import threading
from http.server import ThreadingHTTPServer

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


def test_page_uses_event_source_without_interval_polling() -> None:
    assert "new EventSource('/api/status')" in web.PAGE
    assert "setInterval(" not in web.PAGE


def test_page_routes_repository_views_with_browser_history() -> None:
    assert "history.pushState" in web.PAGE
    assert "addEventListener('popstate',route)" in web.PAGE
    assert "new URLSearchParams(location.search).get('repository')" in web.PAGE
    assert "if(!selected&&repos[0])" not in web.PAGE


def test_page_uses_sans_serif_text_and_monospace_identifiers() -> None:
    assert "font-family:'Noto Sans JP','Yu Gothic',sans-serif" in web.PAGE
    assert ".identifier,.node-file{font-family:ui-monospace" in web.PAGE
    assert '<b class="identifier">${n.name}</b>' in web.PAGE
    assert '<span class="identifier">${r.repository}</span>' in web.PAGE


def test_graph_data_does_not_expand_ambiguous_function_names(tmp_path, monkeypatch) -> None:
    root = tmp_path / "owner" / "demo" / "code"
    root.mkdir(parents=True)
    for file_name in ("a.py", "b.py"):
        (root / f"{file_name}.yaml").write_text(
            f"path: {file_name}\nfunctions:\n  - name: shared\n    calls: []\n",
            encoding="utf-8",
        )
    (root / "caller.py.yaml").write_text(
        "path: caller.py\nfunctions:\n  - name: run\n    calls: [shared]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(web, "OWNER", "owner")

    nodes = {node["id"]: node for node in web.graph_data("demo")["nodes"]}

    assert nodes["caller.py::run"]["calls"] == ["external::shared"]