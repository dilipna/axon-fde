"""Tests for the health endpoints.

The distinction being protected here is that ``/health`` must not depend on
external services. If it did, a database blip would cycle every healthy
instance behind the load balancer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.config import get_settings
from backend.app.main import create_app

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_liveness_reports_configuration_without_touching_dependencies(client):
    """Passes with no containers running - that is the point of the endpoint."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] in {"local", "ci", "demo", "production"}
    assert body["llm_mode"] == "cassette"


def test_liveness_response_carries_no_secrets(client):
    body = client.get("/api/v1/health").text
    for leaked in ("password", "secret", "axon_local_dev", "sk-"):
        assert leaked not in body.lower()


def test_dependency_probe_reports_every_dependency(client):
    """Reachability is reported, never asserted: this runs without containers."""
    response = client.get("/api/v1/health/deps")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] in {"ok", "degraded"}

    names = {d["name"] for d in body["dependencies"]}
    assert {"postgres", "mssql_legacy"} <= names

    for dep in body["dependencies"]:
        assert dep["status"] in {"reachable", "unreachable", "not_configured"}
        # The probe reports host:port only. Credentials must never appear.
        assert "PWD=" not in dep["target"]
        assert "password" not in dep["target"].lower()


def test_dependency_probe_degrades_rather_than_failing(client):
    """A half-started environment must still answer, so it can be diagnosed."""
    assert client.get("/api/v1/health/deps").status_code == 200


def test_openapi_schema_is_generated(client):
    """The TypeScript client is generated from this document, so it has to be
    well formed from the first endpoint onwards."""
    response = client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    assert schema["info"]["title"] == "AxonFDE"
    assert "/api/v1/health" in schema["paths"]
