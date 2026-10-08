from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    """Factory so a test can override limits; every client gets its own in-memory DB."""
    clients: list[TestClient] = []

    def factory(**overrides) -> TestClient:
        settings = Settings(database_url="sqlite://", log_level="WARNING", **overrides)
        client = TestClient(create_app(settings), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def upload(client: TestClient, filename: str, content: bytes, content_type: str | None = None):
    files = {"file": (filename, content, content_type or "application/octet-stream")}
    return client.post("/api/files/", files=files)


@pytest.fixture
def upload_file():
    return upload
