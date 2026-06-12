# Unified Bicep Deployer Design

**Date:** 2026-03-07
**Status:** Approved

## Problem

APIM instance deployment uses sequential `az` CLI calls (6 per instance). With 33 instances this is slow — each instance goes through create → wait → api create → product create → product api add → operation create serially. Two separate scripts (`apimspraycreate.py`, `apimspraycreate_teams.py`) duplicate most of their code.

## Solution

Single `apimcreate.py` script that generates a Bicep template inline and deploys all instances in one `az deployment group create` call. Azure handles parallelism natively.

## CLI Interface

```
python3 apimcreate.py --type login --count 33 --outfile urls.txt
python3 apimcreate.py --type teams --count 10 --outfile teams_urls.txt
python3 apimcreate.py --type both --count 33 --outfile urls.txt --teams-outfile teams_urls.txt
```

**Arguments:**
- `--type {login,teams,both}` — what to deploy
- `--count` — number of instances per type
- `--outfile` — URL output file (login gateways)
- `--teams-outfile` — URL output file (teams gateways, required with `--type both`)
- `--location` — comma-separated regions (same as today)
- `--prefix` — API URL prefix (defaults: `oauth` for login, `teamsmt` for teams)
- `--delete-old` / `--delete-only` — cleanup

## Deployment Architecture

1. Python discovers regions, validates args
2. Creates resource group via `az group create`
3. Generates Bicep template with N APIM instances — each with its API, product, product-API link, and operation(s) as child/dependent resources
4. Writes Bicep to temp file
5. Single `az deployment group create --template-file /tmp/apim_deploy.bicep`
6. Azure provisions all instances in parallel (ARM handles dependency ordering)
7. `az deployment group show` extracts gateway URLs from deployment outputs
8. Writes URLs to outfile, cleans up temp file

### Bicep Structure Per Instance

```
resource apim_N -> Microsoft.ApiManagement/service (Consumption)
  └─ resource api_N -> Microsoft.ApiManagement/service/apis
       └─ resource operation_N -> Microsoft.ApiManagement/service/apis/operations
  └─ resource product_N -> Microsoft.ApiManagement/service/products
  └─ resource productApi_N -> Microsoft.ApiManagement/service/products/apis
```

### `--type both` Behavior

Login and teams instances deploy in a single Bicep template / single deployment. Both types go into the same resource group. Instance names are prefixed differently (`apimspray-*` vs `apimteams-*`). Deployment outputs are split by type and written to respective outfiles.

### Cleanup

`--delete-old` searches for resource groups matching `apim-rotator-*`, `apim-teams-rotator-*` (backward compat), and `apim-deploy-*` (new unified prefix).

## File Changes

- **New:** `apimcreate.py` — unified deployer
- **Keep:** `apimspraycreate.py`, `apimspraycreate_teams.py` — unchanged, still functional
- **Update:** `apimspray.py` — auto-deploy fallback calls `apimcreate.py`
- **Update:** `README.md` — document new script

## Speed Gain

Eliminates ~5 sequential CLI calls per instance post-provisioning. All instances deploy truly in parallel with one ARM operation. The fundamental APIM provisioning time (~2-5 min) is unchanged, but total wall-clock time drops from `O(n * 6 calls)` to `O(1 deployment)`.
