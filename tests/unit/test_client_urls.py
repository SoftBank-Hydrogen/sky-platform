"""Browser-facing endpoint values must not silently point back to a user's laptop."""

import pytest

from application.client_urls import check_browser_client_urls


@pytest.mark.parametrize("target", ["aws-ecs-express", "cloud-run"])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VITE_API_URL", "http://localhost:8080"),
        ("NEXT_PUBLIC_API_URL", "https://127.0.0.1:8080"),
        ("REACT_APP_WS_URL", "ws://example.test/socket"),
        ("PUBLIC_BACKEND_URL", "wss://[::1]/socket"),
        ("CLIENT_SERVICE_URL", "//localhost/api"),
        ("VITE_API_URL", "https://0.0.0.0/api"),
        ("VITE_API_URL", "https://api.example.test\\@localhost/api"),
    ],
)
def test_cloud_rejects_browser_endpoints_that_cannot_work(value, name, target):
    with pytest.raises(ValueError, match="CV-05") as error:
        check_browser_client_urls({name: value}, target)
    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value", ["/api", "https://api.example.test", "wss://game.example.test/ws", "//api.example.test/v1"]
)
def test_cloud_allows_relative_or_secure_external_browser_endpoints(value):
    check_browser_client_urls({"VITE_API_URL": value}, "aws-ecs-express")


def test_server_only_url_is_outside_browser_endpoint_rule():
    check_browser_client_urls({"INTERNAL_SERVICE_URL": "http://localhost:8080"}, "cloud-run")
    check_browser_client_urls({"VITE_API_URL": "http://localhost:8080"}, "local-docker")


def test_malformed_browser_endpoint_is_rejected_without_echoing_it():
    value = "https://[broken"
    with pytest.raises(ValueError, match="CV-05") as error:
        check_browser_client_urls({"VITE_API_URL": value}, "cloud-run")
    assert value not in str(error.value)
