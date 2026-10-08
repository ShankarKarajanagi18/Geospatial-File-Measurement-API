from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def upload(client: TestClient, filename: str, content: bytes, content_type: str | None = None):
    files = {"file": (filename, content, content_type or "application/octet-stream")}
    return client.post("/api/files/", files=files)


def upload_ok(client: TestClient, filename: str, content: bytes) -> dict[str, Any]:
    response = upload(client, filename, content)
    assert response.status_code == 201, response.text
    return response.json()


def measurements(client: TestClient, file_id: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def upload_and_measure(client: TestClient, filename: str, content: bytes) -> list[dict[str, Any]]:
    info = upload_ok(client, filename, content)
    return measurements(client, info["id"])["measurements"]
