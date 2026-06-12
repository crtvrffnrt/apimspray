# Azure Functions HTTP Proxy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy Azure Functions across regions as HTTP proxies to Teams API, providing real IP diversity for fast enumeration (~30 req/s with 15 regions).

**Architecture:** New script `funcproxy.py` deploys one Azure Function App per region (Consumption plan). Each Function contains a minimal Python HTTP trigger that proxies requests to `https://teams.microsoft.com/api/mt/`. URLs are consumed by `apimspray.py` via existing `--teams-urls`. Rate settings updated for IP-diverse scenario.

**Tech Stack:** Python 3, Azure CLI (`az`), Azure Functions Core Tools (for zip deploy), `requests` library.

---

### Task 1: Create the Azure Function proxy code (the function itself)

**Files:**
- Create: `func_template/__init__.py` (the HTTP trigger)
- Create: `func_template/function.json` (trigger binding config)
- Create: `func_template/host.json` (function app settings)
- Create: `func_template/requirements.txt` (function dependencies)

**Step 1: Create the function app directory structure**

The Azure Function needs a specific layout:
```
func_template/
├── host.json
├── requirements.txt
└── proxy/
    ├── __init__.py
    └── function.json
```

Create `func_template/host.json`:
```json
{
  "version": "2.0",
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  }
}
```

Create `func_template/requirements.txt`:
```
azure-functions
requests
```

Create `func_template/proxy/function.json`:
```json
{
  "scriptFile": "__init__.py",
  "bindings": [
    {
      "authLevel": "anonymous",
      "type": "httpTrigger",
      "direction": "in",
      "name": "req",
      "methods": ["get", "post", "put", "delete", "patch"],
      "route": "{*path}"
    },
    {
      "type": "http",
      "direction": "out",
      "name": "$return"
    }
  ]
}
```

Create `func_template/proxy/__init__.py`:
```python
import requests
import azure.functions as func

UPSTREAM = "https://teams.microsoft.com/api/mt"

def main(req: func.HttpRequest) -> func.HttpResponse:
    path = req.route_params.get("path", "")
    url = f"{UPSTREAM}/{path}"

    # Forward all headers except Host
    headers = {k: v for k, v in req.headers.items()
               if k.lower() not in ("host", "content-length")}

    resp = requests.request(
        method=req.method,
        url=url,
        headers=headers,
        data=req.get_body(),
        timeout=30,
    )

    # Strip transfer-encoding (Azure Functions handles its own)
    resp_headers = {k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-encoding")}

    return func.HttpResponse(
        body=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )
```

**Step 2: Verify the files are correct**

Run: `ls -R func_template/`
Expected: Shows `host.json`, `requirements.txt`, `proxy/__init__.py`, `proxy/function.json`

**Step 3: Commit**

```bash
git add func_template/
git commit -m "feat: add Azure Function proxy template for Teams API"
```

---

### Task 2: Create `funcproxy.py` — deploy command

**Files:**
- Create: `funcproxy.py`

**Step 1: Write the deployer script skeleton**

Create `funcproxy.py` with these capabilities:

```python
#!/usr/bin/env python3
"""
funcproxy.py - Deploy Azure Functions as HTTP proxies to Teams API.
Each Function App in a different region gets its own outbound IP,
providing IP diversity for Teams enumeration.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import shutil
import zipfile

# Reuse color/logging from apimcreate.py pattern
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
            raise
        return None

DEFAULT_REGIONS = [
    "eastus", "westeurope", "japaneast", "australiaeast",
    "southeastasia", "northeurope", "westus2", "centralindia",
    "brazilsouth", "canadacentral", "uksouth", "koreacentral",
    "francecentral", "switzerlandnorth", "norwayeast",
]

RG_PREFIX = "funcproxy-"
APP_PREFIX = "funcproxy-"


def create_zip_package():
    """Create a zip of func_template/ for deployment."""
    template_dir = os.path.join(os.path.dirname(__file__), "func_template")
    if not os.path.isdir(template_dir):
        die(f"Function template not found at {template_dir}")

    zip_path = tempfile.mktemp(suffix=".zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(template_dir):
            for f in files:
                full = os.path.join(root, f)
                arcname = os.path.relpath(full, template_dir)
                zf.write(full, arcname)
    return zip_path


def deploy(regions, outfile, prefix):
    """Deploy one Function App per region."""
    timestamp = int(time.time())
    rg_name = f"{RG_PREFIX}{timestamp}"
    rg_location = regions[0]

    # Validate az login
    try:
        run_command("az account show")
    except Exception:
        die("Azure CLI not logged in. Run: az login")

    # Create resource group
    log("info", f"Creating Resource Group: {rg_name} in {rg_location}")
    run_command(f"az group create --name {rg_name} --location {rg_location} "
                f"--tags createdBy=funcproxy")
    log("ok", "Resource Group ready")

    # Create zip package
    zip_path = create_zip_package()
    log("ok", "Function package created")

    urls = []
    for i, region in enumerate(regions):
        app_name = f"{prefix}{timestamp}-{i}"
        storage = f"fpstor{timestamp}{i}"
        # Storage account names: max 24 chars, lowercase alphanumeric only
        storage = storage[:24]

        log("info", f"[{i+1}/{len(regions)}] Deploying {app_name} in {region}...")

        # Create storage account
        run_command(
            f"az storage account create --name {storage} "
            f"--resource-group {rg_name} --location {region} "
            f"--sku Standard_LRS --kind StorageV2"
        )

        # Create function app
        run_command(
            f"az functionapp create --name {app_name} "
            f"--resource-group {rg_name} --location {region} "
            f"--storage-account {storage} "
            f"--consumption-plan-location {region} "
            f"--runtime python --runtime-version 3.11 "
            f"--functions-version 4 --os-type Linux"
        )

        # Deploy code via zip
        run_command(
            f"az functionapp deployment source config-zip "
            f"--name {app_name} --resource-group {rg_name} "
            f"--src {zip_path}"
        )

        # Get the function URL
        url = f"https://{app_name}.azurewebsites.net/api/"
        urls.append(url)
        log("ok", f"[{i+1}/{len(regions)}] {url}")

    os.unlink(zip_path)

    # Write output file
    if outfile:
        with open(outfile, 'w') as f:
            for url in urls:
                f.write(f"{url}\n")
        log("ok", f"URLs written to {outfile}")

    # Summary
    print("-" * 40)
    print(f"Resource Group : {rg_name}")
    print(f"Function Apps  : {len(urls)}")
    print(f"Regions        : {', '.join(regions)}")
    if outfile:
        print(f"Output File    : {outfile}")
    print("-" * 40)
    print()
    for url in urls:
        print(url)

    return urls


def destroy():
    """Delete all funcproxy resource groups."""
    log("info", "Checking for funcproxy resource groups...")
    try:
        groups_json = run_command(
            f"az group list --query \"[?starts_with(name, '{RG_PREFIX}')].name\" -o json"
        )
        groups = json.loads(groups_json) if groups_json else []
    except Exception:
        groups = []

    if not groups:
        log("info", "No funcproxy resource groups found")
        return

    for grp in groups:
        log("info", f"Deleting {grp}...")
        run_command(f"az group delete --name {grp} --yes --no-wait")
    log("ok", f"Queued {len(groups)} resource group(s) for deletion")


def main():
    parser = argparse.ArgumentParser(
        description="funcproxy - Deploy Azure Functions as Teams API proxies",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--deploy", action="store_true",
                        help="Deploy Function Apps across regions")
    parser.add_argument("--destroy", action="store_true",
                        help="Delete all funcproxy resource groups")
    parser.add_argument("--regions", type=str, default=None,
                        help="Comma-separated regions (default: 15 diverse regions)")
    parser.add_argument("--count", type=int, default=None,
                        help="Number of regions to use (default: all specified regions)")
    parser.add_argument("--outfile", type=str, default=None,
                        help="Output file for Function URLs")
    parser.add_argument("--prefix", type=str, default=APP_PREFIX,
                        help="App name prefix (default: funcproxy-)")

    args = parser.parse_args()

    if args.destroy:
        destroy()
        return

    if not args.deploy:
        parser.print_help()
        return

    regions = DEFAULT_REGIONS
    if args.regions:
        regions = [r.strip().lower() for r in args.regions.split(",") if r.strip()]
    if args.count:
        regions = regions[:args.count]

    if not regions:
        die("No regions specified")

    deploy(regions, args.outfile, args.prefix)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Interrupted")
```

**Step 2: Make it executable and test --help**

Run: `chmod +x funcproxy.py && python3 funcproxy.py --help`
Expected: Shows help with `--deploy`, `--destroy`, `--regions`, `--count`, `--outfile` options

**Step 3: Commit**

```bash
git add funcproxy.py
git commit -m "feat: add funcproxy.py deployer for Azure Functions proxy"
```

---

### Task 3: Update apimspray.py rate settings for IP diversity

**Files:**
- Modify: `apimspray.py:55-67` (ENUM_PACE_SETTINGS and TEAMS_ENUM_RATE_PER_TOKEN)
- Modify: `apimspray.py:867-982` (_token_worker 429 handling)

**Step 1: Update ENUM_PACE_SETTINGS**

Change `apimspray.py` lines 55-67 from:
```python
TEAMS_ENUM_RATE_PER_TOKEN = 5.0  # initial req/s per token; auto-adjusts on 429

ENUM_PACE_SETTINGS = {
    "high":    {"rate": 10, "threads_per_token": 8},
    "medium":  {"rate": 5,  "threads_per_token": 5},
    "mid":     {"rate": 5,  "threads_per_token": 5},
    "low":     {"rate": 2,  "threads_per_token": 3},
    "stealth": {"rate": 0.5, "threads_per_token": 1},
}
```

To:
```python
TEAMS_ENUM_RATE_PER_TOKEN = 20.0  # initial req/s per token with IP diversity

ENUM_PACE_SETTINGS = {
    "high":    {"rate": 30, "threads_per_token": 15},
    "medium":  {"rate": 20, "threads_per_token": 10},
    "mid":     {"rate": 20, "threads_per_token": 10},
    "low":     {"rate": 10, "threads_per_token": 5},
    "stealth": {"rate": 3,  "threads_per_token": 3},
}
```

**Step 2: Simplify _token_worker 429 handling**

Replace the 429 block in `_token_worker` (lines 934-953). Currently it does adaptive backoff + re-queue (broken). Change to: sleep for `Retry-After` seconds, then re-queue. No adaptive rate — with IP diversity, 429s are rare and a simple pause is sufficient.

Replace:
```python
            # 429 — halve rate, re-queue user, NO SLEEPING
            if "429" in str(result["error"]):
                new_rate = token_entry.timer.backoff()
                with counters_lock:
                    counters["rate_limited"] += 1
                    rl = counters["rate_limited"]
                if rl == 1 or rl % 25 == 0:
                    print_warn(f"429 x{rl} — rate→{new_rate:.1f} req/s "
                               f"[{token_entry.username}]")
                    sys.stdout.flush()

                if attempt < MAX_REQUEUE:
                    user_queue.put((email, attempt + 1))
                else:
                    with counters_lock:
                        counters["completed"] += 1
                        counters["errors"] += 1
                    progress.increment()
                user_queue.task_done()
                continue
```

With:
```python
            # 429 — sleep Retry-After, then re-queue
            if "429" in str(result["error"]):
                with counters_lock:
                    counters["rate_limited"] += 1
                    rl = counters["rate_limited"]
                if rl == 1 or rl % 25 == 0:
                    print_warn(f"429 x{rl} [{token_entry.username}]")
                    sys.stdout.flush()

                retry_after = result.get("retry_after", 2)
                time.sleep(retry_after)

                if attempt < MAX_REQUEUE:
                    user_queue.put((email, attempt + 1))
                else:
                    with counters_lock:
                        counters["completed"] += 1
                        counters["errors"] += 1
                    progress.increment()
                user_queue.task_done()
                continue
```

**Step 3: Update _make_enum_request to extract Retry-After header**

In `_make_enum_request`, when a 429 is returned, include `retry_after` in the result dict. Find the 429 handling section and add:
```python
retry_after = int(resp.headers.get("Retry-After", 2))
```
And include `"retry_after": retry_after` in the returned dict.

**Step 4: Remove adaptive rate from IntervalTimer**

Simplify `IntervalTimer` — remove `backoff()` and `record_success()` methods, and the `_base_interval` and `_successes_since_backoff` fields. With IP diversity, fixed-rate is correct.

Replace class (lines 179-228):
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

**Step 5: Remove record_success() call from _token_worker**

Delete line 902: `token_entry.timer.record_success()`

**Step 6: Verify the file parses**

Run: `python3 -c "import apimspray; print('OK')"`
Expected: `OK`

**Step 7: Commit**

```bash
git add apimspray.py
git commit -m "feat: update rate settings and simplify 429 handling for IP diversity"
```

---

### Task 4: Update _make_enum_request to return Retry-After

**Files:**
- Modify: `apimspray.py:771-865` (_make_enum_request)

**Step 1: Read the current _make_enum_request**

Read `apimspray.py` lines 771-865 to find where 429 is handled.

**Step 2: Add retry_after extraction**

Find the section that checks for HTTP 429 and add:
```python
retry_after = int(resp.headers.get("Retry-After", 2))
```
Include `"retry_after": retry_after` in the error dict returned for 429s.

**Step 3: Verify**

Run: `python3 -c "import apimspray; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add apimspray.py
git commit -m "feat: extract Retry-After header from 429 responses"
```

---

### Task 5: End-to-end manual test

**Step 1: Deploy functions to 3 regions (quick test)**

Run:
```bash
python3 funcproxy.py --deploy --count 3 --outfile teams_func_urls.txt
```
Expected: 3 Function Apps deployed, URLs written to `teams_func_urls.txt`

**Step 2: Test enumeration with the Function URLs**

Run (with a small user list):
```bash
python3 apimspray.py enumerate \
  --sac-user <sac_user> --sac-pass <sac_pass> \
  --users test_users.txt \
  --teams-urls teams_func_urls.txt \
  --pace medium \
  --output test_output
```
Expected: Enumeration runs at ~6 req/s (3 IPs × 2 req/s), minimal 429s

**Step 3: Verify IP diversity**

Each Function App should show a different outbound IP. Check:
```bash
for url in $(cat teams_func_urls.txt); do
    echo -n "$url -> "
    curl -s "https://httpbin.org/ip" -H "Host: $(echo $url | cut -d/ -f3)"
done
```

**Step 4: Tear down test deployment**

Run: `python3 funcproxy.py --destroy`
Expected: Resource groups queued for deletion

**Step 5: Commit any fixes found during testing**

---

### Task 6: Update .gitignore

**Files:**
- Modify: `.gitignore`

**Step 1: Add entries for function deployment artifacts**

Add to `.gitignore`:
```
teams_func_urls.txt
*.zip
```

**Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: add funcproxy artifacts to .gitignore"
```
