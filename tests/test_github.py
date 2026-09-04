import io
import json
import urllib.error
from http.client import IncompleteRead

from kg_collector.github import GitHubClient


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_query_retries_gateway_timeout(monkeypatch) -> None:
    attempts = 0

    def urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                request.full_url, 504, "Gateway Timeout", {}, io.BytesIO()
            )
        return Response({"data": {"ok": True}})

    monkeypatch.setattr("kg_collector.github.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("kg_collector.github.time.sleep", lambda _: None)

    assert GitHubClient("token").query("query { ok }", {}) == {"ok": True}
    assert attempts == 3


def test_query_retries_incomplete_read(monkeypatch) -> None:
    attempts = 0

    def urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise IncompleteRead(b"", 100)
        return Response({"data": {"ok": True}})

    monkeypatch.setattr("kg_collector.github.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("kg_collector.github.time.sleep", lambda _: None)

    assert GitHubClient("token").query("query { ok }", {}) == {"ok": True}
    assert attempts == 2