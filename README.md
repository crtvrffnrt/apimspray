 <p align="center">
 <img src="Logo.png" width="1000px" alt="Apimspray" />
</p>

<p align="center">
  <a href="https://github.com/jsarkisian/apimspray">
    <img src="https://img.shields.io/badge/GitHub-Repository-black?style=for-the-badge&logo=github">
  </a>
  <a href="https://shell.azure.com/bash">
    <img src="https://img.shields.io/badge/Azure-Cloud%20Shell-0078D4?style=for-the-badge&logo=microsoftazure">
  </a>
  <a href="https://learn.microsoft.com/entra/identity/">
    <img src="https://img.shields.io/badge/Microsoft-Entra%20ID-6264A7?style=for-the-badge&logo=microsoft">
  </a>
</p>

## apimspray

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Azure](https://img.shields.io/badge/Azure-APIM%20%2B%20ACI-blue?logo=microsoftazure)
![License](https://img.shields.io/badge/License-Research--Only-lightgrey)
![Status](https://img.shields.io/badge/Status-Active%20Development-green)

apimspray is a specialized **Entra ID Assessment Toolkit** designed for authorized security research and Red Teaming. It provides two capabilities:

- **Password Spraying** via Azure API Management (APIM) gateways as a distributed, rotating proxy layer
- **User Enumeration** via passive OneDrive URL probing distributed across Azure Container Instances (ACI)

---

Read [Blog Post](https://patrickbinder.medium.com/entra-id-password-spraying-using-apim-as-ip-rotating-mechanism-3620861dd66a) for more Details about this project
## Prerequisites

- **Python 3.10+**
- **Azure CLI (`az`)**: Required to deploy proxy resources. Must be authenticated (`az login`) before running any deployer script.
- **Active Azure Subscription**: APIM Consumption tier and ACI costs are negligible for short assessments.

### Installation

```bash
git clone https://github.com/jsarkisian/apimspray.git
cd apimspray
pip install -r requirements.txt
az login
```

---

## Tool Overview

| Script | Purpose |
|--------|---------|
| `apimspray.py` | Main tool — spray, validate, enumerate |
| `apimcreate.py` | Deploy APIM gateways for spray/validate |
| `onedrive_proxy.py` | Deploy ACI proxies for user enumeration |

---

## Mode 1: User Enumeration (OneDrive)

Passively checks whether users exist by probing their OneDrive URL. No authentication required — does not trigger login events or lockouts.

**How it works:** A valid user returns HTTP 403 (OneDrive exists but access denied). An invalid user returns 404.

**NOTE: You will need the tenant name before you can enumerate users properly. Refer to the following for more info:**

Easy Lookup: https://sub2tenant.com/<BR>
Trimarc Details: https://www.trimarcsecurity.com/hub-post/enumerating-entra-id-anonymously<BR>
AADInternals Tool: https://aadinternals.com/aadinternals/

### Step 1: Deploy ACI Proxies

ACI containers are deployed across Azure regions, each with a unique public IP, to distribute enumeration traffic.

```bash
# Deploy 50 proxies (auto-distributes across all US regions)
python3 onedrive_proxy.py --deploy --tenant contoso.com --count 50 --outfile enum_urls.txt

# Deploy into specific regions
python3 onedrive_proxy.py --deploy --tenant contoso.com --count 30 \
    --regions eastus,westus,westeurope --outfile enum_urls.txt

# Clean up when done
python3 onedrive_proxy.py --destroy
```

> The deployer verifies the tenant exists before spending time on Azure resources. If the SharePoint host is unreachable, it aborts immediately.

**onedrive_proxy CLI reference:**

```text
usage: onedrive_proxy.py [--deploy] [--destroy] [--delete-old]
                         [--tenant TENANT] [--domain DOMAIN]
                         [--regions REGIONS] [--count COUNT] [--outfile OUTFILE]

options:
  --deploy              Deploy ACI containers
  --destroy             Delete all odproxy resource groups
  --delete-old          Delete old odproxy resource groups before deploying
  --tenant TENANT       Target tenant/domain (e.g. contoso.com)
  --domain DOMAIN       Domain hint (required if --tenant is a UUID)
  --regions REGIONS     Comma-separated regions (default: all US regions)
  --count COUNT         Number of containers to deploy (default: 10)
  --outfile OUTFILE     Output file for proxy URLs
```

Default regions (when `--regions` is not set): `eastus`, `eastus2`, `westus`, `westus2`, `westus3`, `centralus`, `northcentralus`, `southcentralus`, `westcentralus`

> **Quota note:** Azure limits ACI to 10 CPU cores per region (~20 containers at 0.5 CPU each). Spreading across regions avoids hitting this limit.

### Step 2: Run Enumeration

```bash
# With proxies (recommended) — no --tenant needed, proxies are already configured
python3 apimspray.py --mode enumerate --users users.txt --aci-urls enum_urls.txt --enum-pace turbo

# Without proxies (single IP, slower)
python3 apimspray.py --mode enumerate --users users.txt --tenant contoso.com
```

### Enumeration Speed Templates (`--enum-pace`)

Thread count scales automatically with proxy count. Timeout is fixed per template.

| Template | Threads/Proxy | Timeout | Threads with 50 proxies | Threads with 25 proxies |
|----------|:---:|:---:|:---:|:---:|
| `turbo`  | 20 | 30s | 1000 | 500 |
| `high`   | 10 | 25s | 500  | 250 |
| `medium` |  5 | 20s | 250  | 125 |
| `low`    |  2 | 15s | 100  |  50 |
| `stealth`|  1 | 10s |  50  |  25 |

```bash
python3 apimspray.py --mode enumerate --users users.txt --aci-urls enum_urls.txt --enum-pace turbo
```
If you notice weird behavior during enumeration, run enumeration with the --verbose flag and see if you are getting timeouts. If so, lower your pace. The tool will also retry the user guesses that timed out.

### Enumeration Options

```text
  --aci-urls ACI_URLS          Path to ACI proxy URLs file (from onedrive_proxy.py)
  --enum-pace {turbo,high,medium,low,stealth}
                               Speed template — overrides --threads and --timeout
  --threads THREADS            Number of threads (default: 100). Overridden by --enum-pace.
  --timeout TIMEOUT            Request timeout in seconds (default: 5). Overridden by --enum-pace.
  --retries N                  Retry errored users N times at end of run (default: 1)
  --tenant TENANT              Required only when not using --aci-urls
  --domain DOMAIN              Append domain to users if missing
  --randomize-users            Shuffle user list before enumeration
  --verbose                    Print per-request debug output (URL, status, errors)
  --output OUTPUT              Output directory (default: results)
```

### Enumeration Output

Results are stored in `results/<timestamp>/`:

- `enumerated_<timestamp>.txt` — confirmed valid users (one per line)

Valid users are also printed in real-time during the run:
```
[+] VALID: john.doe@contoso.com
```

Progress is printed every 10 seconds:
```
[*] Progress: 12000/48000 | Found: 34 | 312.4 req/s | ETA: 2m03s
```

---

## Mode 2: Password Spraying / Validation (APIM)

Uses Azure API Management gateways as rotating proxies to spray passwords against Entra ID without triggering IP-based lockouts.

### Step 1: Deploy APIM Gateways

```bash
# Deploy 33 login gateways
python3 apimcreate.py --type login --count 33 --outfile urls.txt

# Deploy into specific regions
python3 apimcreate.py --type login --location germanywestcentral,westeurope --count 33 --outfile urls.txt

# Clean up
python3 apimcreate.py --type login --delete-only
```

**apimcreate CLI reference:**

```text
usage: apimcreate.py [-h] --type {login,teams,both} [--count COUNT] [--outfile OUTFILE]
                     [--location LOCATION] [--prefix PREFIX] [--delete-old] [--delete-only]

options:
  --type {login,both}   Type of APIM gateways to deploy
  --count COUNT         Number of instances per type
  --outfile OUTFILE     Output file for gateway URLs
  --location LOCATION   Comma-separated APIM location(s)
  --prefix PREFIX       API URL prefix (default: oauth)
  --delete-old          Delete old resource groups before deploying
  --delete-only         Only delete old resource groups
```

### Step 2: Spray or Validate

**spray** — one password tested against all users, repeats for each password:
```bash
python3 apimspray.py --mode spray \
    --urls urls.txt \
    --users users.txt \
    --passwords passwords.txt \
    --pace medium \
    --tenant contoso.com \
    --remove-disabled
```

> `--remove-disabled` drops any account that returns `AADSTS50057` (disabled) from all subsequent password rounds, reducing wasted attempts.

**validate** — 1:1 user:password pair testing (equal-length lists):
```bash
python3 apimspray.py --mode validate \
    --urls urls.txt \
    --users users.txt \
    --passwords passwords.txt \
    --tenant contoso.com
```

### Spray Pacing Profiles (`--pace`)

| Profile   | Workers | Delay | Chunk | Lockout Wait | Safe Threshold | Jitter |
|:----------|:-------:|:-----:|:-----:|:------------:|:--------------:|:------:|
| `high`    |   15    | 0.1s  |  10   |     5m       |   20 locked    |   0%   |
| `medium`  |    5    | 1.0s  |   5   |    10m       |   10 locked    |  10%   |
| `low`     |    2    | 5.0s  |   2   |    15m       |    5 locked    |  20%   |
| `stealth` |    1    | 30.0s |   1   |    20m       |    1 locked    |  40%   |

- **Chunk**: Passwords attempted per user before pausing for lockout timers to reset
- **Lockout Wait**: Sleep time between chunks
- **Safe Threshold**: Abort if this many accounts hit `AADSTS50053` (Smart Lockout)
- **Jitter**: Randomizes delay to avoid static timing detection

### Spray/Validate Options

```text
  --urls URLS                  Path to APIM gateway URLs file
  --users USERS                Path to users file
  --passwords PASSWORDS        Path to passwords file
  --tenant TENANT              Tenant ID or domain (default: common)
  --domain DOMAIN              Append domain to users if missing
  --pace {stealth,low,medium,high}
                               Pacing profile (default: low)
  --continue-on-success        Keep spraying after finding valid credentials
  --remove-disabled            Remove disabled accounts (AADSTS50057) from the
                               spray list as they are discovered
  --randomize-users            Shuffle user order before each round
  --verbose                    Enable progress output (press Enter to print status)
  --output OUTPUT              Output directory (default: results)
```

### Spray/Validate Output

Results are stored in `results/<timestamp>/`:

- `valid_*.txt` — successful authentications (MFA required or token received)
- `blocked_*.txt` — locked or conditionally blocked accounts
- `failed_*.txt` — invalid credentials or user not found

---

## Typical Workflow

```bash
# 1. Deploy ACI proxies for enumeration
python3 onedrive_proxy.py --deploy --tenant contoso.com --count 50 --outfile enum_urls.txt

# 2. Enumerate valid users
python3 apimspray.py --mode enumerate --users users.txt --aci-urls enum_urls.txt --enum-pace turbo

# 3. Deploy APIM gateways for spraying
python3 apimcreate.py --type login --count 33 --outfile spray_urls.txt

# 4. Spray enumerated users
python3 apimspray.py --mode spray \
    --urls spray_urls.txt \
    --users results/<timestamp>/enumerated_<timestamp>.txt \
    --passwords passwords.txt \
    --tenant contoso.com \
    --pace medium

# 5. Clean up
python3 onedrive_proxy.py --destroy
python3 apimcreate.py --type login --delete-only
```

---

## Credits & References

- **[crtvrffnrt/apimspray](https://github.com/crtvrffnrt/apimspray)**: Original tool and core concept — this project is built on their work
- **o365spray**: Logic inspiration for ROPC flow
- **nyxgeek/onedrive_user_enum**: OneDrive enumeration technique
- **TeamFiltration**: Design inspiration for tool structure
- **FireProx**: The grandfather of cloud gateway rotation techniques
