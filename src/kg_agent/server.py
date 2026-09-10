from __future__ import annotations

import json
import logging
import os
import select
import socket
import urllib.error
import urllib.request
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from .graphrag import GraphRAGAgent
except ImportError:
    from graphrag import GraphRAGAgent

COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://kg-collector:8081").rstrip("/")
NEO4J_URL = os.environ.get("NEO4J_URL", "http://neo4j:7474").rstrip("/")
NEO4J_BOLT_HOST = os.environ.get("NEO4J_BOLT_HOST", "neo4j")
NEO4J_BOLT_PORT = int(os.environ.get("NEO4J_BOLT_PORT", "7687"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "dist"))
MAX_CHAT_HISTORY = 20
MAX_CHAT_MESSAGE_LENGTH = 8_000
_agent: GraphRAGAgent | None = None
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("kg_agent.server")


def get_agent() -> GraphRAGAgent:
    global _agent
    if _agent is None:
        _agent = GraphRAGAgent.from_env()
    return _agent


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy(self, base_url: str = COLLECTOR_URL, path: str | None = None) -> None:
        try:
            self._proxy(base_url, path)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _proxy(self, base_url: str, path: str | None) -> None:
        body = None
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            body = self.rfile.read(content_length)
        request = urllib.request.Request(
            f"{base_url}{path or self.path}",
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                self.send_response(response.status)
                content_type = response.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", content_type)
                if "text/event-stream" in content_type:
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    while chunk := response.readline():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    payload = response.read()
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.send_response(error.code)
            self.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except urllib.error.URLError as error:
            self.send_json(
                {"error": f"Upstream service is unavailable: {error.reason}"},
                HTTPStatus.BAD_GATEWAY,
            )

    def proxy_neo4j(self) -> None:
        path = self.path.removeprefix("/neo4j") or "/"
        self.proxy(NEO4J_URL, path)

    def proxy_bolt_websocket(self) -> None:
        try:
            with socket.create_connection((NEO4J_BOLT_HOST, NEO4J_BOLT_PORT), timeout=10) as upstream:
                request = f"{self.command} {self.path} {self.request_version}\r\n"
                headers = "".join(f"{name}: {value}\r\n" for name, value in self.headers.items())
                upstream.sendall(f"{request}{headers}\r\n".encode("iso-8859-1"))
                sockets = (self.connection, upstream)
                while True:
                    readable, _, _ = select.select(sockets, [], [])
                    for source in readable:
                        payload = source.recv(65536)
                        if not payload:
                            return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(payload)
        except OSError:
            self.close_connection = True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.proxy_bolt_websocket()
            return
        if path.startswith("/api/"):
            self.proxy()
            return
        if path == "/neo4j" or path.startswith("/neo4j/"):
            self.proxy_neo4j()
            return
        requested = self.translate_path(path)
        if Path(requested).is_file():
            super().do_GET()
            return
        self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/chat":
            self.chat()
        elif path == "/api/collect":
            self.proxy()
        elif path.startswith("/neo4j/"):
            self.proxy_neo4j()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def chat(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > 200_000:
            self.send_json({"error": "質問を入力してください"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            message = payload.get("message", "").strip()
            if not message:
                raise ValueError
        except (json.JSONDecodeError, AttributeError, ValueError):
            self.send_json({"error": "message を含むJSONを送信してください"}, HTTPStatus.BAD_REQUEST)
            return
        history = normalize_chat_history(payload.get("history"))
        LOGGER.info("chat request started: message=%r history=%d", message[:200], len(history))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send_event(event: dict[str, Any]) -> None:
            try:
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.warning("chat client disconnected while sending %s", event.get("type"), exc_info=True)
                raise

        try:
            result = get_agent().answer(
                message,
                lambda progress: self._send_chat_progress(send_event, progress),
                history=history,
            )
            send_event({"type": "result", **result})
            LOGGER.info(
                "chat request completed: message=%r sources=%d",
                message[:200],
                len(result.get("sources", [])),
            )
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            LOGGER.exception("chat request failed: message=%r", message[:200])
            try:
                send_event({"type": "error", "error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_chat_progress(self, send_event: Any, progress: dict[str, Any]) -> None:
        LOGGER.info("chat progress: %s", json.dumps(progress, ensure_ascii=False, default=str))
        send_event({"type": "progress", **progress})

    def log_message(self, format: str, *args: object) -> None:
        return


def normalize_chat_history(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    normalized = []
    for item in history[-MAX_CHAT_HISTORY:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "agent"}:
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        normalized.append({
            "role": item["role"],
            "text": text.strip()[:MAX_CHAT_MESSAGE_LENGTH],
        })
    return normalized


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    handler = partial(Handler, directory=str(STATIC_DIR))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"kg-agent listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()