# Auto-cleanup of Expired Azure Resource Groups

## Problem

Users forget to destroy Azure resources after spraying/enumerating, leading to large bills from idle APIM gateways and ACI containers.

## Solution

Automatically delete resource groups older than 8 hours every time `apimcreate.py --deploy` or `onedrive_proxy.py --deploy` runs. No new flags, scripts, or infrastructure.

## Design

### Mechanism

Both scripts already embed a Unix timestamp in resource group names:
- APIM: `apim-deploy-{timestamp}`, `apim-rotator-{timestamp}`, `apim-teams-rotator-{timestamp}`
- OneDrive: `odproxy-{timestamp}`

On every `--deploy` invocation, before creating new resources:

1. List resource groups matching known prefixes
2. Parse the Unix timestamp from each group name
3. Delete any where `now - timestamp > TTL_SECONDS` (default 28800 = 8 hours)
4. Log deletions so the user sees what was cleaned up

### Constants

Each script defines `TTL_SECONDS = 28800` for easy tuning.

### Scope

- `apimcreate.py`: checks prefixes `apim-deploy-`, `apim-rotator-`, `apim-teams-rotator-`
- `onedrive_proxy.py`: checks prefix `odproxy-`

### What doesn't change

- `--delete-old` / `--destroy` flags remain for manual cleanup of all resources regardless of age
- Spray, validate, and enumerate modes are untouched
- No new CLI flags, Azure tags, or external dependencies

## Approach chosen

Timestamp-from-name parsing (Approach A) over Azure tags (B) or tags + keep-alive (C). Simplest change, no new dependencies, works retroactively with existing resource groups.
