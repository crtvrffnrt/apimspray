import pytest
import sys
import os
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onedrive_enum import OneDriveEnumerator, build_onedrive_path


# --- URL construction ---

def test_simple_upn():
    path = build_onedrive_path("john.doe@contoso.com")
    assert path == "personal/john_doe_contoso_com/_layouts/15/onedrive.aspx"

def test_hyphen_in_username():
    path = build_onedrive_path("john-doe@contoso.com")
    assert path == "personal/john_doe_contoso_com/_layouts/15/onedrive.aspx"

def test_dots_in_domain():
    path = build_onedrive_path("alice@sub.contoso.com")
    assert path == "personal/alice_sub_contoso_com/_layouts/15/onedrive.aspx"

def test_uppercase_normalised():
    path = build_onedrive_path("John.Doe@Contoso.COM")
    assert path == "personal/john_doe_contoso_com/_layouts/15/onedrive.aspx"


# --- HTTP check ---

def _make_enumerator():
    return OneDriveEnumerator(proxy_urls=[])


def test_check_user_valid():
    """403 response means valid user."""
    enum = _make_enumerator()
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch("onedrive_enum.requests.get", return_value=mock_resp):
        result = enum._check_user("john.doe@contoso.com", "contoso")
    assert result == "valid"


def test_check_user_not_found():
    """404 response means user not found."""
    enum = _make_enumerator()
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("onedrive_enum.requests.get", return_value=mock_resp):
        result = enum._check_user("nobody@contoso.com", "contoso")
    assert result == "not_found"


def test_check_user_error_on_exception():
    """Network exception returns 'error'."""
    import requests as req
    enum = _make_enumerator()
    with patch("onedrive_enum.requests.get", side_effect=req.RequestException("timeout")):
        result = enum._check_user("john.doe@contoso.com", "contoso")
    assert result == "error"


def test_check_user_via_proxy():
    """When proxy_url provided, request goes to proxy URL not direct."""
    enum = _make_enumerator()
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    captured = {}
    def fake_get(url, **kwargs):
        captured["url"] = url
        return mock_resp
    with patch("onedrive_enum.requests.get", side_effect=fake_get):
        enum._check_user("john@contoso.com", "contoso", proxy_url="http://1.2.3.4:8080/")
    assert captured["url"].startswith("http://1.2.3.4:8080/")
    assert "personal/" in captured["url"]


def test_enumerate_with_proxies():
    """enumerate() distributes work across proxies and returns valid users."""
    proxy_urls = ["http://1.1.1.1:8080/", "http://2.2.2.2:8080/"]
    enum = OneDriveEnumerator(proxy_urls=proxy_urls)

    call_count = [0]
    def fake_check(upn, tenant_name, proxy_url=None, session=None):
        call_count[0] += 1
        if upn == "alice@contoso.com":
            return "valid"
        return "not_found"

    class FakeLogger:
        def __init__(self):
            self.logged = []
        def log_result(self, result_type, value):
            self.logged.append((result_type, value))

    logger = FakeLogger()
    users = ["alice@contoso.com", "bob@contoso.com"]

    with patch.object(enum, "_check_user", side_effect=fake_check):
        valid_users, counters = enum.enumerate(users, "contoso", logger)

    assert "alice@contoso.com" in valid_users
    assert "bob@contoso.com" not in valid_users
    assert counters["valid"] == 1
    assert counters["not_found"] == 1
    assert counters["completed"] == 2
    assert ("enumerated", "alice@contoso.com") in logger.logged
