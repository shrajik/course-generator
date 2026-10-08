"""The executor's HTTP contract."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the service needs root to install the firewall"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_green_only_with_network_isolation(client) -> None:
    body = client.get("/health")
    assert body.status_code == 200, body.text
    assert body.json()["network_isolated"] is True


def test_languages_lists_only_installed_runtimes(client) -> None:
    languages = {item["id"]: item for item in client.get("/languages").json()}
    assert set(languages) == {"python", "javascript", "cpp", "java"}
    assert languages["cpp"]["label"] == "C++"
    assert "c++" in languages["cpp"]["aliases"]


def test_execute_returns_the_normalised_result(client) -> None:
    response = client.post("/execute", json={"language": "python", "code": "print(10 + 20)"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["stdout"] == "30\n"
    assert body["stderr"] == ""
    assert body["language"] == "python"
    assert isinstance(body["execution_time"], float)


def test_aliases_resolve_to_the_canonical_language(client) -> None:
    response = client.post("/execute", json={"language": "C++", "code": "int main(){return 0;}"})
    assert response.status_code == 200
    assert response.json()["language"] == "cpp"


def test_unknown_language_is_rejected(client) -> None:
    response = client.post("/execute", json={"language": "cobol", "code": "x"})
    assert response.status_code == 400


def test_network_access_cannot_be_requested(client) -> None:
    """A deployment capability, not something a request can switch on."""
    response = client.post(
        "/execute", json={"language": "python", "code": "print(1)", "network_enabled": True}
    )
    assert response.status_code == 400


def test_oversized_code_is_rejected(client) -> None:
    response = client.post(
        "/execute", json={"language": "python", "code": "#" * (config.MAX_CODE_BYTES + 1)}
    )
    assert response.status_code == 413


def test_requested_limits_are_clamped_to_the_ceiling(client) -> None:
    response = client.post(
        "/execute",
        json={"language": "python", "code": "print('ok')", "timeout": 9999, "memory_limit_mb": 99999},
    )
    assert response.status_code == 200


def test_unknown_fields_are_rejected(client) -> None:
    response = client.post(
        "/execute", json={"language": "python", "code": "x", "run_as_root": True}
    )
    assert response.status_code == 422
