 <p align="center">
 <img src="Logo.png" width="1000px" alt="Apimspray" />
</p>

<p align="center">
  <a href="https://github.com/crtvrffnrt/apimspray">
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
![Azure](https://img.shields.io/badge/Azure-APIM-blue?logo=microsoftazure)
![License](https://img.shields.io/badge/License-Research--Only-lightgrey)
![Status](https://img.shields.io/badge/Status-Active%20Development-green)

apimspray is a specialized **Entra ID Passwordspraying Toolkit** designed for authorized security research and Red Teaming. It utilizes Azure API Management (APIM) gateways as a distributed, rotating proxy layer for IP Rotating.

Read [Blog Post](https://patrickbinder.medium.com/entra-id-password-spraying-using-apim-as-ip-rotating-mechanism-3620861dd66a) for more Details about this project
## Prerequisites

- **Azure CLI (`az`)**: Required for the rotator script to deploy resources. (Execute script from cli session already authenticated to az cli or use Azure Cloud Shell)
- **Active Azure Subscription**: To deploy APIM Consumption tier resources (Cost is negligible, typically <$0.01 for short assessments).

### Installation

1. Clone the repository.
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Ensure you are logged into Azure CLI:
   ```bash
   az login
   ```
   _Note: The rotator script relies on an active background Azure CLI session to deploy resources._

## Setup

### Quick Start (Azure Cloud Shell)

Run apimspray directly from an authenticated Azure environment.

<a href="https://shell.azure.com/bash">
  <img src="https://az-icons.com/api/icon/azure-cloud-shell/download?format=png"
       alt="Open in Azure Cloud Shell"
       width="180" />
</a>

```bash
git clone https://github.com/crtvrffnrt/apimspray.git
cd apimspray
python3 apimspray.py --help
```

### 1. Deploy Gateways

Deploy multiple APIM Gateways into various Locations

```bash
python3 apimspraycreate.py --count 5 --outfile urls.txt
```

Deploys 33 APIM instances into Germanywestcentral and westeurope and saves to urls.txt

```bash
 python3 apimspraycreate.py --location germanywestcentral,westeurope --count 33 --outfile urls.txt
```

**apimspraycreate CLI reference:**

```text
usage: apimspraycreate.py [-h] --outfile OUTFILE [--count COUNT] [--location LOCATION] [--prefix PREFIX] [--realm-prefix REALM_PREFIX] [--delete-old]

apimspraycreate - Azure APIM Deployer

options:
  -h, --help            show this help message and exit
  --outfile OUTFILE     Output file for URLs
  --count COUNT         Number of instances
  --location LOCATION   Comma-separated APIM location(s) to deploy into. When provided, only those regions are used and the first location is used for the resource group.
  --prefix PREFIX       API URL prefix
  --realm-prefix REALM_PREFIX
                        Realm API prefix
  --delete-old          Delete old resource groups
```

**Important:** For all methods, you must have an active `az` session in the background (`az login`).

### 2. Prepare Wordlists

Ensure you have your target lists ready:

- `users.txt`: List of UserPrincipalNames (e.g., `user@domain.com`).
- `passwords.txt`: List of passwords to spray.

### 3. Reconnaissance / UPN Generation (Optional)

If you do not have a user list, you can use the helper script `generate_upns.py`. This tool:

1. Queries the target Azure tenant to discover all connected and verified domains.
2. Downloads a list of statistically likely service account usernames from GitHub.
3. Generates a permutation list of UPNs (UserPrincipalNames) and saves them to `users.txt`.

**Usage:**

```bash
# Generate users.txt for a specific domain or tenant ID
python3 generate_upns.py --target example.com
# OR using Tenantid
python3 generate_upns.py --target 00000000-0000-0000-0000-000000000000
```

**generate_upns CLI reference:**

```text
usage: generate_upns.py [-h] --target TARGET

Generate UPNs from connected Azure Tenants and Service Accounts.

options:
  -h, --help       show this help message and exit
  --target TARGET  Target Domain (e.g., example.com) or Tenant UUID
```

## Usage

```text
usage: apimspray.py [-h] [--urls URLS] [--users USERS] [--passwords PASSWORDS] [--output OUTPUT] [--tenant TENANT] [--domain DOMAIN] --mode {spray,validate} [--pace {stealth,low,mid,medium,high}]
                    [--continue-on-success]

apimspray - Entra ID Assessment Tool

options:
  -h, --help            show this help message and exit
  --urls URLS           Path to APIM URLs file (from apimspraycreate.py or apimsprayrotator.sh)
  --users USERS         Path to users file
  --passwords PASSWORDS
                        Path to passwords file
  --output OUTPUT       Output directory
  --tenant TENANT       Tenant ID or Domain
  --domain DOMAIN       Append domain to users if missing
  --mode {spray,validate}
                        Operation mode. 'spray' tests all passwords against all users (1:N). 'validate' performs 1:1 credential pair testing.
  --pace {stealth,low,mid,medium,high}
                        Pacing profile for requests and lockout management:
                         - high:    15 workers, 0.1s delay, 10 passes/chunk, 5m lockout, 20 safe threshold
                         - medium:  5 workers,  1.0s delay,  5 passes/chunk, 10m lockout, 10 safe threshold, 10% jitter
                         - low:     2 workers,  5.0s delay,  2 passes/chunk, 15m lockout,  5 safe threshold, 20% jitter
                         - stealth: 1 worker,  30.0s delay,  1 pass/chunk,   20m lockout,  1 safe threshold, 40% jitter
  --continue-on-success
                        Continue the assessment even after finding valid credentials.
```

### Modes

- **validate**: Checks a list of `user:password` pairs. Requires equal length lists.

  ```bash
  python3 apimspray.py --urls urls.txt --mode validate --users u.txt --passwords p.txt
  ```

- **spray**: Attempts one password against all users, then waits (if configured), then moves to the next password.
  ```bash
  python3 apimspray.py --urls urls.txt --mode spray --users users.txt --passwords common_passwords.txt --pace medium
  ```

### Pacing Profiles

The `--pace` argument controls the aggressiveness of the spray. Values are hardcoded to ensure stability and safety.

| Profile   | Workers | Delay | Count (Chunk) | Lockout Wait | Safe Threshold | Jitter |
| :-------- | :-----: | :---: | :-----------: | :----------: | :------------: | :----: |
| `high`    |   15    | 0.1s  |      10       |      5m      |   20 locked    |   0%   |
| `medium`  |    5    | 1.0s  |       5       |     10m      |   10 locked    |  10%   |
| `low`     |    2    | 5.0s  |       2       |     15m      |    5 locked    |  20%   |
| `stealth` |    1    | 30.0s |       1       |     20m      |    1 locked    |  40%   |

- **Count**: Number of passwords to try per user before pausing to let lockout timers reset.
- **Lockout Wait**: Time to sleep between password chunks.
- **Safe Threshold**: If this many accounts get locked (`AADSTS50053`), the tool aborts immediately.
- **Jitter**: Randomizes the delay to evade static timing analysis.

## Output

Results are stored in the `results/<timestamp>/` directory:

- `valid_*.txt`: Successful authentications (MFA Required or Token received).
- `blocked_*.txt`: Locked or conditionally blocked accounts.
- `failed_*.txt`: Invalid credentials or user not found.

## Credits & References

- **o365spray**: Logic inspiration for ROPC flow.
- **TeamFiltration**: Design inspiration for tool structure.
- **FireProx**: The grandfather of cloud gateway rotation techniques.
