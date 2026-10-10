"""Task protection cannot call arbitrary URLs or follow redirects."""

from unittest.mock import MagicMock, Mock

import pytest

from adapters.aws.task_protection import EcsTaskProtection, NoRedirect


@pytest.mark.parametrize(
    "uri", ["", "https://evil.test", "http://169.254.170.2/path", "http://169.254.170.2@evil.test"]
)
def test_endpoint_is_pinned(uri):
    with pytest.raises(ValueError):
        EcsTaskProtection(uri)


def test_only_matching_protection_acknowledgement_is_accepted():
    protection = EcsTaskProtection("http://169.254.170.2")
    response = Mock()
    response.read.return_value = b'{"protection":{"ProtectionEnabled":true}}'
    protection.opener = MagicMock()
    protection.opener.open.return_value.__enter__.return_value = response
    protection.set(True)
    request = protection.opener.open.call_args.args[0]
    assert request.full_url == "http://169.254.170.2/task-protection/v1/state"
    assert request.method == "PUT"
    with pytest.raises(OSError):
        protection.set(False)


def test_redirect_is_never_followed():
    assert NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://evil.test") is None


@pytest.mark.parametrize("body", [b"[]", b'{"protection":[]}', b"x" * 8193])
def test_malformed_agent_response_is_a_safe_failure(body):
    protection = EcsTaskProtection("http://169.254.170.2")
    protection.opener = MagicMock()
    protection.opener.open.return_value.__enter__.return_value.read.return_value = body
    with pytest.raises(OSError, match="Task protection unavailable"):
        protection.set(True)
