from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from .cli import main as collect, repository_matches_glob
from .storage import Neo4jGraphStore, graph_store_from_env

OWNER = os.environ.get("GH_TARGET_ORGANIZATION", "")
REPOSITORY_GLOB = os.environ.get("TARGET_REPOSITORY_GLOB", "")
store_lock = threading.Lock()
store: Neo4jGraphStore | None = None
collection_lock = threading.RLock()
collection_condition = threading.Condition(collection_lock)
collection_state: dict[str, Any] = {
    "running": False,
    "message": "待機中",
    "repository": "",
    "completed": 0,
    "total": 0,
}
collection_revision = 0


def update_collection_state(**changes: Any) -> None:
    global collection_revision
    with collection_condition:
        collection_state.update(changes)
        collection_revision += 1
        collection_condition.notify_all()


def graph_store() -> Neo4jGraphStore:
    global store
    with store_lock:
        if store is None:
            store = graph_store_from_env()
            store.wait_until_ready()
            store.ensure_schema()
        return store


def graph_data(repository: str) -> dict[str, Any]:
    return graph_store().graph_data(OWNER, repository)


def start_collection() -> bool:
    with collection_condition:
        if collection_state["running"]:
            return False
        update_collection_state(
            running=True,
            message="収集中...",
            repository="準備中",
            completed=0,
            total=0,
        )

    def progress(message: str, repository: str) -> None:
        with collection_condition:
            changes: dict[str, Any] = {"message": message, "repository": repository}
            if message in {"完了", "失敗"}:
                changes["completed"] = collection_state["completed"] + 1
            if message == "準備中":
                try:
                    changes["total"] = int(repository.split()[0])
                except (ValueError, IndexError):
                    pass
            update_collection_state(**changes)

    def run() -> None:
        try:
            result = collect([], progress=progress)
            message = "収集完了" if result == 0 else "収集に失敗しました"
            update_collection_state(running=False, message=message, repository="")
        except Exception as error:
            update_collection_state(
                running=False,
                message=f"収集に失敗しました: {error}",
                repository="",
            )

    threading.Thread(target=run, name="kg-collection", daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/repositories":
                data = graph_store().repositories(OWNER)
                data["repositories"] = [
                    repository
                    for repository in data["repositories"]
                    if repository_matches_glob(repository["name"], REPOSITORY_GLOB)
                ]
                data["repository_glob"] = REPOSITORY_GLOB
                self.send_json(data)
            elif path.startswith("/api/repositories/"):
                repository = unquote(path.removeprefix("/api/repositories/"))
                detail = graph_store().repository_detail(OWNER, repository)
                if detail is None:
                    self.send_json({"error": "Repository not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(detail)
            elif path == "/api/status":
                self.send_status_events()
            elif path.startswith("/api/graph/"):
                repository = unquote(path.removeprefix("/api/graph/"))
                self.send_json(graph_data(repository))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (Neo4jError, ServiceUnavailable, OSError, ValueError) as error:
            self.send_json(
                {"error": "Neo4j is unavailable", "detail": str(error)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def send_status_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        revision = -1
        try:
            while True:
                with collection_condition:
                    changed = collection_condition.wait_for(
                        lambda: collection_revision != revision,
                        timeout=15,
                    )
                    if changed:
                        revision = collection_revision
                        data = json.dumps(collection_state, ensure_ascii=False)
                message = f"data: {data}\n\n" if changed else ": keep-alive\n\n"
                self.wfile.write(message.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/api/collect":
            self.send_json({"started": start_collection()}, HTTPStatus.ACCEPTED)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.environ.get("PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Collector API listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()