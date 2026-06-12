# OneDrive Enumeration Integration — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Teams-based `enumerate` mode with passive OneDrive HTTP enumeration distributed across ACI proxy IPs.

**Architecture:** A new `onedrive_enum.py` module contains `OneDriveEnumerator`, which checks users by GETting OneDrive URLs and reading HTTP status codes (403=valid, 404=not found). `onedrive_proxy.py` deploys ACI containers targeting SharePoint. `apimspray.py` is surgically modified: Teams code is deleted, `--mode enumerate` is re-wired to `OneDriveEnumerator`, and `aci_template/app.py` is made generic via a `TARGET_HOST` env var.

**Tech Stack:** Python 3.10+, `requests`, Azure CLI (`az`), Azure Container Instances, Flask (existing container image)

---

## Chunk 1: Container + Proxy Deployer + Enumerator Module

### Task 1: Make ACI container proxy generic

**Files:**
- Modify: `aci_template/app.py`

The container image currently hardcodes `https://teams.microsoft.com/api/mt` as the upstream. Replace with an env var so it can proxy any host.

- [ ] **Step 1: Read the current file**

```bash
cat aci_template/app.py
```

- [ ] **Step 2: Replace hardcoded UPSTREAM with env var, and enable redirect following**

Two changes to `aci_template/app.py`:
1. Replace hardcoded `UPSTREAM` with a `TARGET_HOST` env var (primary change)
2. Add `allow_redirects=True` to the proxy request so that upstream redirects (common on SharePoint) are followed transparently before returning the final status to the caller

Full file after edit:
```python
from flask import Flask, request, Response
import requests as req_lib
import os

app = Flask(__name__)

TARGET_HOST = os.environ.get("TARGET_HOST", "teams.microsoft.com")
UPSTREAM = f"https://{TARGET_HOST}"

@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy(path):
    url = f"{UPSTREAM}/{path}" if path else UPSTREAM
    headers = {k: v for k, v in request.headers if k.lower() not in ("host", "content-length")}
    try:
        resp = req_lib.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            timeout=30,
            allow_redirects=True,
        )
    except req_lib.RequestException as e:
        return Response(str(e), status=502)
    excluded = {"transfer-encoding", "content-encoding", "content-length"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    return Response(resp.content, status=resp.status_code, headers=resp_headers)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

- [ ] **Step 3: Verify file parses correctly**

```bash
python -c "import ast; ast.parse(open('aci_template/app.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add aci_template/app.py
git commit -m "feat: make ACI proxy target configurable via TARGET_HOST env var"
```

---

### Task 2: Create `onedrive_proxy.py` deployer

**Files:**
- Create: `onedrive_proxy.py`

Deploys ACI containers configured to proxy `[tenant]-my.sharepoint.com`. Structurally identical to `aciproxy.py` but with different defaults, resource group prefix, and a required `--tenant` argument. The `TARGET_HOST` env var is passed at container create time.

- [ ] **Step 1: Create tests directory**

```bash
mkdir -p tests
```

- [ ] **Step 2: Write failing test for tenant name derivation**

Create `tests/test_onedrive_proxy.py`:
```python
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onedrive_proxy import derive_sharepoint_host

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
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /root/tools/apimspray && python -m pytest tests/test_onedrive_proxy.py -v
```
Expected: `ModuleNotFoundError: No module named 'onedrive_proxy'`

- [ ] **Step 4: Create `onedrive_proxy.py` with `derive_sharepoint_host` and deploy logic**

```python
#!/usr/bin/env python3
"""
onedrive_proxy.py - Deploy Azure Container Instances as HTTP proxies for OneDrive user enumeration.
Each container gets a unique public Azure IP, enabling parallel enumeration across multiple IPs.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time


class Colors:
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    RESET = '\033[0m'


def log(level, message):
    colors = {"info": Colors.BLUE, "ok": Colors.GREEN,
              "warn": Colors.YELLOW, "error": Colors.RED}
    labels = {"info": "[INFO]", "ok": "[ OK ]",
              "warn": "[WARN]", "error": "[ERR ]"}
    print(f"{colors.get(level, '')}{labels.get(level, '')}{Colors.RESET} {message}")


def die(message):
    log("error", message)
    sys.exit(1)


def run_command(command, check=True):
    try:
        result = subprocess.run(command, check=check, shell=True,
                                text=True, capture_output=True)
        return result.stdout.strip() if result.stdout else ""
    except subprocess.CalledProcessError as e:
        if check:
            if e.stderr:
                log("error", e.stderr.strip())
            raise
        return None


DEFAULT_REGIONS = ["eastus"]
RG_PREFIX = "odproxy-"
ACR_PREFIX = "odproxyreg"

UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def derive_sharepoint_host(tenant, domain=None):
    """
    Derive the SharePoint hostname from a tenant string.

    Examples:
      contoso.com             -> contoso-my.sharepoint.com
      contoso.onmicrosoft.com -> contoso-my.sharepoint.com
      contoso                 -> contoso-my.sharepoint.com
      <UUID>                  -> requires domain arg
    """
    if UUID_RE.match(tenant):
        if not domain:
            raise ValueError(
                "Tenant is a UUID — provide --domain to derive SharePoint hostname"
            )
        name = domain.split(".")[0]
    elif "." in tenant:
        name = tenant.split(".")[0]
    else:
        name = tenant
    return f"{name}-my.sharepoint.com"


def deploy(tenant, domain, regions, count, outfile):
    """Deploy ACI containers as OneDrive enum proxies."""
    sharepoint_host = derive_sharepoint_host(tenant, domain)
    timestamp = int(time.time())
    rg_name = f"{RG_PREFIX}{timestamp}"
    acr_name = f"{ACR_PREFIX}{timestamp}"
    rg_location = regions[0]

    try:
        run_command("az account show")
    except Exception:
        die("Azure CLI not logged in. Run: az login")

    reg_state = run_command(
        "az provider show --namespace Microsoft.ContainerInstance "
        "--query registrationState -o tsv", check=False)
    if reg_state and reg_state.strip() != "Registered":
        log("info", "Registering Microsoft.ContainerInstance provider...")
        run_command("az provider register --namespace Microsoft.ContainerInstance")
        for _ in range(30):
            time.sleep(5)
            state = run_command(
                "az provider show --namespace Microsoft.ContainerInstance "
                "--query registrationState -o tsv", check=False)
            if state and state.strip() == "Registered":
                break
        else:
            die("Microsoft.ContainerInstance provider did not register in time.")
        log("ok", "Provider registered")

    log("info", f"Target: {sharepoint_host}")
    log("info", f"Creating Resource Group: {rg_name} in {rg_location}")
    run_command(f"az group create --name {rg_name} --location {rg_location} "
                f"--tags createdBy=odproxy")
    log("ok", "Resource Group ready")

    log("info", f"Creating Container Registry: {acr_name}")
    run_command(f"az acr create --name {acr_name} --resource-group {rg_name} "
                f"--location {rg_location} --sku Basic --admin-enabled true")
    log("ok", "Container Registry ready")

    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aci_template")
    if not os.path.isdir(template_dir):
        die(f"Container template not found at {template_dir}")

    image_name = f"{acr_name}.azurecr.io/onedriveproxy:latest"
    log("info", "Building container image via ACR (remote build)...")
    run_command(f"az acr build --registry {acr_name} --resource-group {rg_name} "
                f"--image onedriveproxy:latest {template_dir}")
    log("ok", f"Image built: {image_name}")

    creds_json = run_command(
        f"az acr credential show --name {acr_name} --resource-group {rg_name} -o json")
    creds = json.loads(creds_json)
    acr_user = creds["username"]
    acr_pass = creds["passwords"][0]["value"]
    acr_server = f"{acr_name}.azurecr.io"

    results = [None] * count
    errors = []
    lock = threading.Lock()
    completed = [0]

    def deploy_container(i):
        region = regions[i % len(regions)]
        container_name = f"odproxy-{timestamp}-{i}"
        tag = f"[{i+1}/{count}] {region}"
        try:
            log("info", f"{tag}: Deploying container {container_name}...")
            run_command(
                f"az container create "
                f"--resource-group {rg_name} "
                f"--name {container_name} "
                f"--image {image_name} "
                f"--cpu 0.5 --memory 0.5 "
                f"--ports 8080 "
                f"--os-type Linux "
                f"--ip-address Public "
                f"--location {region} "
                f"--registry-login-server {acr_server} "
                f"--registry-username {acr_user} "
                f"--registry-password '{acr_pass}' "
                f"--environment-variables TARGET_HOST={sharepoint_host} "
                f"--restart-policy Never"
            )
            ip = run_command(
                f"az container show --resource-group {rg_name} "
                f"--name {container_name} "
                f"--query ipAddress.ip -o tsv"
            )
            if ip and ip.strip():
                url = f"http://{ip.strip()}:8080/"
                results[i] = url
                with lock:
                    completed[0] += 1
                    log("ok", f"{tag}: Ready ({completed[0]}/{count}) — {url}")
            else:
                with lock:
                    completed[0] += 1
                    errors.append(container_name)
                    log("error", f"{tag}: No IP assigned ({completed[0]}/{count})")
        except Exception as e:
            with lock:
                completed[0] += 1
                errors.append(container_name)
                log("error", f"{tag}: Failed ({completed[0]}/{count}) — {e}")

    log("info", f"Deploying {count} containers in parallel (target: {sharepoint_host})...")
    threads = []
    for i in range(count):
        t = threading.Thread(target=deploy_container, args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    urls = [u for u in results if u is not None]
    if errors:
        log("warn", f"{len(errors)} container(s) failed: {', '.join(errors)}")

    if not urls:
        die("No containers deployed successfully")

    if outfile:
        with open(outfile, 'w') as f:
            for url in urls:
                f.write(f"{url}\n")
        log("ok", f"URLs written to {outfile}")

    print("-" * 40)
    print(f"Resource Group : {rg_name}")
    print(f"Registry       : {acr_name}")
    print(f"Target Host    : {sharepoint_host}")
    print(f"Containers     : {len(urls)}")
    print(f"Regions        : {', '.join(regions)}")
    if outfile:
        print(f"Output File    : {outfile}")
    print("-" * 40)
    for url in urls:
        print(url)

    return urls


def destroy():
    """Delete all odproxy resource groups."""
    log("info", "Checking for odproxy resource groups...")
    try:
        groups_json = run_command(
            f"az group list --query \"[?starts_with(name, '{RG_PREFIX}')].name\" -o json")
        groups = json.loads(groups_json) if groups_json else []
    except Exception:
        groups = []

    if not groups:
        log("info", "No odproxy resource groups found")
        return

    for grp in groups:
        log("info", f"Deleting {grp}...")
        run_command(f"az group delete --name {grp} --yes --no-wait")
    log("ok", f"Queued {len(groups)} resource group(s) for deletion")


def main():
    parser = argparse.ArgumentParser(
        description="onedrive_proxy - Deploy ACI containers for OneDrive user enumeration",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--deploy", action="store_true",
                        help="Deploy ACI containers")
    parser.add_argument("--destroy", action="store_true",
                        help="Delete all odproxy resource groups")
    parser.add_argument("--delete-old", action="store_true",
                        help="Delete old odproxy resource groups before deploying")
    parser.add_argument("--tenant", type=str, default=None,
                        help="Target tenant/domain (e.g. contoso.com). Determines SharePoint host.")
    parser.add_argument("--domain", type=str, default=None,
                        help="Domain hint (required if --tenant is a UUID)")
    parser.add_argument("--regions", type=str, default=None,
                        help="Comma-separated regions (default: eastus)")
    parser.add_argument("--count", type=int, default=10,
                        help="Number of containers to deploy (default: 10)")
    parser.add_argument("--outfile", type=str, default=None,
                        help="Output file for proxy URLs")

    args = parser.parse_args()

    if args.destroy:
        destroy()
        return

    if args.delete_old:
        destroy()

    if not args.deploy:
        parser.print_help()
        return

    if not args.tenant:
        die("--tenant is required for deployment (e.g. --tenant contoso.com)")

    regions = DEFAULT_REGIONS
    if args.regions:
        regions = [r.strip().lower() for r in args.regions.split(",") if r.strip()]

    if not regions:
        die("No regions specified")

    deploy(args.tenant, args.domain, regions, args.count, args.outfile)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Interrupted")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /root/tools/apimspray && python -m pytest tests/test_onedrive_proxy.py -v
```
Expected: 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add onedrive_proxy.py tests/test_onedrive_proxy.py
git commit -m "feat: add onedrive_proxy.py deployer for OneDrive enum ACI containers"
```

---

### Task 3: Create `onedrive_enum.py` enumerator module

**Files:**
- Create: `onedrive_enum.py`
- Create: `tests/test_onedrive_enum.py`

This module contains the core enumeration logic: URL construction, HTTP checks, and the threaded worker pool.

- [ ] **Step 1: Write failing tests**

Create `tests/test_onedrive_enum.py`:
```python
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


def test_check_user_valid(tmp_path):
    """403 response means valid user."""
    enum = _make_enumerator()
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch("onedrive_enum.requests.get", return_value=mock_resp):
        result = enum._check_user("john.doe@contoso.com", "contoso")
    assert result == "valid"


def test_check_user_not_found(tmp_path):
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/tools/apimspray && python -m pytest tests/test_onedrive_enum.py -v
```
Expected: `ModuleNotFoundError: No module named 'onedrive_enum'`

- [ ] **Step 3: Create `onedrive_enum.py`**

```python
#!/usr/bin/env python3
"""
onedrive_enum.py - OneDrive-based user enumeration for apimspray.

Checks whether users have OneDrive provisioned by making GET requests to:
  https://[tenant]-my.sharepoint.com/personal/[user]_[domain]_com/_layouts/15/onedrive.aspx

Response interpretation:
  403 Forbidden  -> valid user (OneDrive provisioned)
  404 Not Found  -> user not found or OneDrive never accessed
  302 redirect   -> followed transparently by allow_redirects=True; final status evaluated
  other          -> unknown / error
"""

import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# User agents for GET requests
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]

_SAFE_RE = re.compile(r'[.\-]')


def build_onedrive_path(upn):
    """
    Build the OneDrive personal URL path for a given UPN.

    john.doe@contoso.com -> personal/john_doe_contoso_com/_layouts/15/onedrive.aspx
    """
    upn = upn.lower()
    username, domain = upn.split("@", 1)
    username_safe = _SAFE_RE.sub("_", username)
    domain_safe = domain.replace(".", "_")
    return f"personal/{username_safe}_{domain_safe}/_layouts/15/onedrive.aspx"


class OneDriveEnumerator:
    """
    Enumerates valid users via OneDrive URL probing.

    Uses a thread pool with one thread per proxy URL.
    Falls back to a single direct thread when no proxies are provided.
    """

    def __init__(self, proxy_urls=None):
        self.proxy_urls = list(proxy_urls) if proxy_urls else []

    def _check_user(self, upn, tenant_name, proxy_url=None):
        """
        Make a single OneDrive check for a UPN.

        Returns: 'valid', 'not_found', or 'error'
        """
        path = build_onedrive_path(upn)
        if proxy_url:
            url = f"{proxy_url.rstrip('/')}/{path}"
        else:
            url = f"https://{tenant_name}-my.sharepoint.com/{path}"

        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            if resp.status_code == 403:
                return "valid"
            elif resp.status_code == 404:
                return "not_found"
            else:
                return "error"
        except requests.RequestException:
            return "error"

    def enumerate(self, users, tenant_name, logger):
        """
        Enumerate all users using the proxy pool.

        Thread model: one thread per proxy URL, each thread owns its proxy.
        Returns: (valid_users: set, counters: dict)
        """
        user_queue = queue.Queue()
        for u in users:
            user_queue.put(u)

        proxy_pool = self.proxy_urls if self.proxy_urls else [None]
        n_threads = len(proxy_pool)

        valid_users = set()
        results_lock = threading.Lock()
        counters = {"completed": 0, "valid": 0, "not_found": 0, "errors": 0}

        def worker(proxy_url):
            while True:
                try:
                    upn = user_queue.get_nowait()
                except queue.Empty:
                    break
                result = self._check_user(upn, tenant_name, proxy_url)
                with results_lock:
                    counters["completed"] += 1
                    if result == "valid":
                        counters["valid"] += 1
                        valid_users.add(upn)
                        logger.log_result("enumerated", upn)
                    elif result == "not_found":
                        counters["not_found"] += 1
                    else:
                        counters["errors"] += 1
                user_queue.task_done()

        with ThreadPoolExecutor(max_workers=n_threads) as executor:
            futures = [executor.submit(worker, proxy_pool[i % len(proxy_pool)])
                       for i in range(n_threads)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        return valid_users, counters
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/tools/apimspray && python -m pytest tests/test_onedrive_enum.py -v
```
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onedrive_enum.py tests/test_onedrive_enum.py
git commit -m "feat: add OneDriveEnumerator module with threaded proxy pool"
```

---

## Chunk 2: Modify `apimspray.py`

### Task 4: Remove Teams code and wire `--mode enumerate` to OneDriveEnumerator

**Files:**
- Modify: `apimspray.py` (surgical deletions + targeted additions)

This is the largest task. Follow the steps in order — each step removes or replaces one specific block.

**Reference — line ranges to delete (verify with `grep -n` before editing):**

| Block | Approx lines | Content |
|-------|-------------|---------|
| Teams enum pacing | 52–67 | `TEAMS_ENUM_RATE_PER_TOKEN`, `ENUM_PACE_SETTINGS` |
| Teams client config | 109–126 | `TEAMS_CLIENT_CONFIG`, `TEAMS_REGIONS`, `DEFAULT_TEAMS_REGION`, `TEAMS_CLIENT_HEADERS` |
| `IntervalTimer` class | 179–207 | (only used by Teams) |
| `_SacToken` class | 209–224 | (only used by Teams) |
| `TeamsAPIMManager` class | 227–249 | |
| `TeamsEnumerator` class | ~548–1069 | |
| `_run_enumerate` function | ~1438–1682 | Replace entirely |
| `_print_enum_summary` function | ~1704–1733 | Replace with simpler version |

- [ ] **Step 1: Delete Teams pacing constants (lines ~52–67)**

Remove this block from `apimspray.py`:
```python
# Enumerate pacing (separate from spray — enum is read-only, no lockout risk)
# Teams API throttles per token. Each token gets dedicated worker threads with
# an IntervalTimer for fixed-rate spacing. Add more sacrificial accounts to scale.
TEAMS_ENUM_RATE_PER_TOKEN = 2.0  # base req/s per IP; auto-scaled by proxy count

# rate_per_ip = max req/s to send through each proxy IP
# Teams tolerates ~2-3 req/s per source IP. Total throughput is computed at
# runtime: rate_per_ip × num_proxies / num_tokens → per-token rate.
# threads_per_token scales with rate to overlap HTTP latency.
ENUM_PACE_SETTINGS = {
    "high":    {"rate_per_ip": 2.5, "threads_per_token_per_ip": 1},
    "medium":  {"rate_per_ip": 1.5, "threads_per_token_per_ip": 1},
    "mid":     {"rate_per_ip": 1.5, "threads_per_token_per_ip": 1},
    "low":     {"rate_per_ip": 0.8, "threads_per_token_per_ip": 1},
    "stealth": {"rate_per_ip": 0.3, "threads_per_token_per_ip": 1},
}
```

- [ ] **Step 2: Delete Teams client config constants (lines ~109–126)**

Remove this block:
```python
# Teams-specific client config for sacrificial account auth
TEAMS_CLIENT_CONFIG = {
    "client_id": "1fec8e78-bce4-4aaf-ab1b-5451cc387264",
    "resource": "https://api.spaces.skype.com/",
    "scope": "openid profile"
}

# Teams API regions
TEAMS_REGIONS = ["amer", "emea", "apac"]
DEFAULT_TEAMS_REGION = "amer"

# Teams client headers (mimics Android Teams app, same as TeamFiltration)
TEAMS_CLIENT_HEADERS = {
    "x-ms-client-caller": "x-ms-client-caller",
    "x-ms-client-version": "27/1.0.0.2021011237",
    "Referer": "https://teams.microsoft.com/_",
    "ClientInfo": "os=Android; osVer=7.1.2; proc=x86; lcid=en-US; deviceType=2; country=US; clientName=microsoftteams; clientVer=1416/1.0.0.2021012201; utcOffset=+01:00"
}
```

- [ ] **Step 3: Delete `IntervalTimer` class (lines ~179–207)**

Remove:
```python
class IntervalTimer:
    """
    Ensures requests are spaced at least `interval` seconds apart.
    Multiple threads share one timer per token.
    """
    __slots__ = ("interval", "_next_slot", "_lock")

    def __init__(self, rate):
        self.interval = 1.0 / rate
        self._next_slot = time.monotonic()
        self._lock = threading.Lock()

    def wait(self):
        """Block until the next rate-limit slot is available."""
        with self._lock:
            now = time.monotonic()
            if now < self._next_slot:
                wait_time = self._next_slot - now
                self._next_slot += self.interval
            else:
                wait_time = 0
                self._next_slot = now + self.interval
        if wait_time > 0:
            time.sleep(wait_time)

    @property
    def current_rate(self):
        return 1.0 / self.interval
```

- [ ] **Step 4: Delete `_SacToken` class (lines ~209–224)**

Remove:
```python
class _SacToken:
    """
    Holds auth state for a single sacrificial account:
    bearer token, skype token, credentials (for re-auth), and a per-token
    IntervalTimer for rate spacing.
    """
    __slots__ = ("bearer", "skype", "username", "password", "timer",
                 "reauth_lock")

    def __init__(self, bearer, skype, username, password="", rate=None):
        self.bearer = bearer
        self.skype = skype
        self.username = username
        self.password = password
        self.timer = IntervalTimer(rate or TEAMS_ENUM_RATE_PER_TOKEN)
        self.reauth_lock = threading.Lock()
```

- [ ] **Step 5: Delete `TeamsAPIMManager` class (lines ~227–249)**

Remove:
```python
class TeamsAPIMManager:
    """Manages APIM URLs specifically configured for Teams API proxying."""
    def __init__(self, urls):
        self.urls = urls
        self.lock = threading.Lock()
        self._pool = []
        self._last_url = None

    def get_next_url(self):
        """Returns the next Teams APIM URL, cycling through a shuffled pool."""
        with self.lock:
            if not self.urls:
                raise ValueError("No Teams APIM URLs available.")
            if len(self.urls) == 1:
                return self.urls[0]
            if not self._pool:
                self._pool = list(self.urls)
                random.shuffle(self._pool)
                if self._last_url and self._pool[0] == self._last_url and len(self._pool) > 1:
                    self._pool[0], self._pool[1] = self._pool[1], self._pool[0]
            next_url = self._pool.pop(0)
            self._last_url = next_url
            return next_url
```

- [ ] **Step 6: Delete `TeamsEnumerator` class (~lines 548–1069)**

Find the class start with:
```bash
grep -n "^class TeamsEnumerator" apimspray.py
```
Find the class end (next top-level `def` or `class` after it) with:
```bash
grep -n "^def \|^class " apimspray.py | grep -A1 "TeamsEnumerator"
```
Delete everything between `class TeamsEnumerator:` and the next top-level definition.

- [ ] **Step 7: Delete old `_run_enumerate` AND `_print_enum_summary` functions**

Locate the exact line ranges first:
```bash
grep -n "^def _run_enumerate\|^def _print_enum_summary\|^def _print_summary" apimspray.py
```
Expected output shows three lines, e.g.:
```
1438:def _run_enumerate(args):
1684:def _print_summary(logger, locked_users, invalid_users):
1704:def _print_enum_summary(logger, valid_users, all_users, counters):
```

Delete everything from `def _run_enumerate` through the end of `_print_enum_summary` — i.e. from line 1438 to the last line of `_print_enum_summary` (the line containing the closing `print(style("---...` call). Keep `_print_summary` (used by spray/validate) and everything after it intact.

In practice: delete from line 1438 up to (but not including) `def _print_summary`. The new `_run_enumerate` and new `_print_enum_summary` added in Step 8 replace both deleted functions.

- [ ] **Step 8: Add new `_run_enumerate` and `_print_enum_summary` functions**

Add these two functions at the same location where `_run_enumerate` was (before `_print_summary`):

```python
def _run_enumerate(args):
    """Execute OneDrive-based user enumeration."""
    from onedrive_enum import OneDriveEnumerator, build_onedrive_path
    from onedrive_proxy import derive_sharepoint_host

    if not args.users:
        print_error("Enumerate mode requires --users (file of candidate email addresses)")
        sys.exit(1)

    users = load_file_lines(args.users)
    users = normalize_users(users, args.domain)
    if not users:
        print_error("No users loaded from file")
        sys.exit(1)
    if args.randomize_users:
        random.shuffle(users)
        print_info(f"User list randomized ({len(users)} users shuffled)")

    try:
        tenant_name = derive_sharepoint_host(args.tenant, args.domain).split("-my.")[0]
    except ValueError as e:
        print_error(str(e))
        sys.exit(1)

    proxy_urls = []
    if getattr(args, "aci_urls", None) and Path(args.aci_urls).exists():
        proxy_urls = load_file_lines(args.aci_urls)

    logger = Logger(args.output)

    print_info(f"Starting apimspray")
    print_info(f"Mode: {style('enumerate', TermColors.MAGENTA, TermColors.BOLD)} (OneDrive User Enumeration)")
    print_info(f"Tenant: {style(tenant_name, TermColors.CYAN, TermColors.BOLD)}")
    print_info(f"Candidate Users: {style(str(len(users)), TermColors.MAGENTA, TermColors.BOLD)}")
    if proxy_urls:
        print_info(f"ACI Proxies: {style(str(len(proxy_urls)), TermColors.MAGENTA, TermColors.BOLD)} (one thread per IP)")
    else:
        print_info("No --aci-urls provided — enumeration going direct (single thread)")

    enumerator = OneDriveEnumerator(proxy_urls)
    valid_users, counters = enumerator.enumerate(users, tenant_name, logger)

    _print_enum_summary(logger, valid_users, users, counters)


def _print_enum_summary(logger, valid_users, all_users, counters):
    print("\n" + style("--- Enumeration Summary ---", TermColors.BOLD, TermColors.CYAN))
    print(f"Total Candidates:    {style(str(len(all_users)), TermColors.BOLD)}")
    print(f"Completed:           {style(str(counters['completed']), TermColors.BOLD)}")
    print(f"Valid Users Found:   {style(str(counters['valid']), TermColors.GREEN, TermColors.BOLD)}")
    print(f"Not Found:           {style(str(counters['not_found']), TermColors.DIM)}")
    print(f"Errors/Timeouts:     {style(str(counters['errors']), TermColors.YELLOW, TermColors.BOLD)}")
    coverage = (counters["completed"] / len(all_users) * 100) if all_users else 0
    print(f"Coverage:            {style(f'{coverage:.1f}%', TermColors.CYAN, TermColors.BOLD)}")
    print(f"Results Directory:   {style(str(logger.run_dir), TermColors.CYAN, TermColors.BOLD)}")
    if logger.files["enumerated"].exists():
        print(f"Valid Users File:    {style(str(logger.files['enumerated']), TermColors.GREEN)}")
    print(style("----------------------------", TermColors.BOLD, TermColors.CYAN))
```

- [ ] **Step 9: Update CLI args in `main()`**

**Remove** these `add_argument` calls:
- `--teams-urls`
- `--sac-user`
- `--sac-pass`
- `--sac-accounts`
- `--teams-region`
- `--no-presence`
- `--skip-sanity`

**Add** this arg (in the enumerate-specific section):
```python
parser.add_argument(
    "--aci-urls",
    help="Path to ACI proxy URLs file for enumeration (from onedrive_proxy.py).\n"
         "Each proxy URL gets one dedicated thread. More proxies = higher throughput."
)
```

**Update** the tool description string (line ~1203):
```python
description="apimspray - Entra ID Assessment Tool (with OneDrive Enumeration)",
```

**Update** the `--mode enumerate` help text:
```python
" - enumerate:  Enumerate valid users via OneDrive URL probing (passive, no login).\n"
"               Route through ACI proxies (--aci-urls) for multi-IP throughput."
```

- [ ] **Step 10: Verify the tool loads and help works**

```bash
cd /root/tools/apimspray && python apimspray.py --help
```
Expected: help text shows, no mention of Teams/sac/presence/sanity, `--aci-urls` is listed under enumerate options.

```bash
python apimspray.py --help 2>&1 | grep -E "teams-urls|sac-user|sac-pass|sac-accounts|teams-region|no-presence|skip-sanity"
```
Expected: no output (none of those args exist any more).

```bash
python apimspray.py --help 2>&1 | grep "aci-urls"
```
Expected: one line showing `--aci-urls` in the help text.

- [ ] **Step 11: Run all tests**

```bash
cd /root/tools/apimspray && python -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 12: Commit**

```bash
git add apimspray.py
git commit -m "feat: replace Teams enumeration with OneDrive passive user enumeration

- Remove TeamsEnumerator, TeamsAPIMManager, _SacToken, IntervalTimer
- Remove all sacrificial account, Teams region, presence, sanity check args
- Add --aci-urls for OneDrive enum proxy pool
- Wire --mode enumerate to OneDriveEnumerator
"
```

---

## Final Verification

- [ ] **Smoke test enumerate mode (dry run)**

```bash
echo "john.doe@contoso.com" > /tmp/test_users.txt
python apimspray.py --mode enumerate --users /tmp/test_users.txt --tenant contoso.com --output /tmp/test_results
```
Expected: prints startup info, attempts OneDrive check, exits cleanly.

- [ ] **Smoke test spray mode (ensure unchanged)**

```bash
python apimspray.py --mode spray --help
```
Expected: help renders, `--urls`, `--pace`, spray options all still present.
