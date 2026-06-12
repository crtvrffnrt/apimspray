import pytest
import sys
import os
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onedrive_proxy import derive_sharepoint_host, verify_tenant, discover_sharepoint_host

def test_domain_tenant():
    assert derive_sharepoint_host("contoso.com") == "contoso-my.sharepoint.com"

def test_onmicrosoft_tenant():
    assert derive_sharepoint_host("contoso.onmicrosoft.com") == "contoso-my.sharepoint.com"

def test_bare_tenant():
    assert derive_sharepoint_host("contoso") == "contoso-my.sharepoint.com"

def test_uuid_tenant_with_domain():
    assert derive_sharepoint_host(
        "12345678-1234-1234-1234-123456789abc", domain="contoso.com"
    ) == "contoso-my.sharepoint.com"

def test_uuid_tenant_without_domain():
    with pytest.raises(ValueError):
        derive_sharepoint_host("12345678-1234-1234-1234-123456789abc")


# --- verify_tenant ---

def test_verify_tenant_valid_on_403():
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch("onedrive_proxy.requests.get", return_value=mock_resp):
        ok, status = verify_tenant("contoso-my.sharepoint.com")
    assert ok is True
    assert status == 403

def test_verify_tenant_valid_on_302():
    mock_resp = MagicMock()
    mock_resp.status_code = 302
    with patch("onedrive_proxy.requests.get", return_value=mock_resp):
        ok, status = verify_tenant("contoso-my.sharepoint.com")
    assert ok is True

def test_verify_tenant_invalid_on_404():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("onedrive_proxy.requests.get", return_value=mock_resp):
        ok, status = verify_tenant("nonexistent-my.sharepoint.com")
    assert ok is False
    assert status == 404

def test_verify_tenant_invalid_on_exception():
    import requests as req
    with patch("onedrive_proxy.requests.get", side_effect=req.RequestException("timeout")):
        ok, status = verify_tenant("nonexistent-my.sharepoint.com")
    assert ok is False
    assert status is None


# --- discover_sharepoint_host ---

def test_discover_simple_prefix_works():
    """If simple prefix resolves, discovery returns immediately."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch("onedrive_proxy.requests.get", return_value=mock_resp):
        host, method = discover_sharepoint_host("contoso.com")
    assert host == "contoso-my.sharepoint.com"
    assert method == "domain prefix"


def test_discover_via_mx_record():
    """Falls back to MX record when simple prefix returns 404."""
    def fake_get(url, **kwargs):
        resp = MagicMock()
        if "contoso-my.sharepoint.com" in url:
            resp.status_code = 404
        elif "getuserrealm" in url:
            resp.status_code = 200
            resp.json.return_value = {"NameSpaceType": "Managed", "FederationBrandName": ""}
        elif "openid-configuration" in url:
            resp.status_code = 200
            resp.json.return_value = {"issuer": "https://sts.windows.net/abc-123/"}
        elif "contosowidgets-my.sharepoint.com" in url:
            resp.status_code = 403
        else:
            resp.status_code = 404
        return resp

    fake_dig = MagicMock()
    fake_dig.returncode = 0
    fake_dig.stdout = "10 contosowidgets.mail.protection.outlook.com.\n"

    with patch("onedrive_proxy.requests.get", side_effect=fake_get), \
         patch("subprocess.run", return_value=fake_dig):
        host, method = discover_sharepoint_host("contoso.com")
    assert host == "contosowidgets-my.sharepoint.com"
    assert "MX" in method


def test_discover_unknown_namespace():
    """Returns None if domain is not a Microsoft 365 tenant."""
    def fake_get(url, **kwargs):
        resp = MagicMock()
        if "sharepoint.com" in url:
            resp.status_code = 404
        elif "getuserrealm" in url:
            resp.status_code = 200
            resp.json.return_value = {"NameSpaceType": "Unknown", "FederationBrandName": ""}
        else:
            resp.status_code = 404
        return resp

    with patch("onedrive_proxy.requests.get", side_effect=fake_get):
        host, method = discover_sharepoint_host("notreal.xyz")
    assert host is None
    assert method is None
