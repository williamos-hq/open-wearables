"""Regression tests for the import-only Garmin fork boundary."""

import pytest
from fastapi.testclient import TestClient

GARMIN_WEBHOOK_ROUTES = (
    "/api/v1/providers/garmin/webhooks",
    "/api/v1/garmin/webhooks/push",
    "/api/v1/garmin/webhooks/ping",
)


@pytest.mark.parametrize("path", GARMIN_WEBHOOK_ROUTES)
def test_garmin_webhook_routes_are_unavailable(client: TestClient, path: str) -> None:
    response = client.post(
        path,
        headers={"garmin-client-id": "obsolete-client-id"},
        json={"activities": []},
    )

    assert response.status_code == 404


def test_garmin_webhook_challenge_is_unavailable(client: TestClient) -> None:
    assert client.get("/api/v1/providers/garmin/webhooks").status_code == 404


def test_garmin_webhook_subscription_management_is_unavailable(client: TestClient) -> None:
    assert client.get("/api/v1/providers/garmin/webhooks/subscriptions").status_code == 404


def test_unknown_provider_still_returns_404(client: TestClient) -> None:
    response = client.post("/api/v1/providers/unknown_provider/webhooks", json={})
    assert response.status_code == 404
