# OneDrive Enumeration Integration — Design Spec

**Date:** 2026-03-10
**Status:** Approved

## Summary

Replace the Teams-based `enumerate` mode in `apimspray.py` with a passive OneDrive enumeration method. Use ACI proxy containers to distribute requests across multiple Azure IPs for throughput scaling.

## Goals

1. Integrate OneDrive user enumeration (inspired by nyxgeek/onedrive_user_enum) as the sole enumeration method.
2. Remove all Teams enumeration code and associated infrastructure from the enumerate path.
3. Enable multi-IP enumeration via ACI proxies to scale throughput linearly with proxy count.
4. Keep spray/validate modes completely unchanged.

## Architecture

### Components Changed

| Component | Change |
|-----------|--------|
| `apimspray.py` | Remove Teams classes/args, delegate `--mode enumerate` to `OneDriveEnumerator` |
| `onedrive_enum.py` | New module — contains `OneDriveEnumerator` class |
| `onedrive_proxy.py` | New script — deploys ACI containers targeting SharePoint |

### Components Unchanged

- `apimcreate.py` — APIM deployer for spray
- `aciproxy.py` — ACI deployer for spray (login.microsoftonline.com)
- `funcproxy.py` — Azure Functions deployer for spray
- `generate_upns.py` — UPN generation helper (still useful for building user lists)

## OneDrive Enumeration Logic (`onedrive_enum.py`)

### URL Construction

Transform UPN `john.doe@contoso.com` into:
```
https://contoso-my.sharepoint.com/personal/john_doe_contoso_com/_layouts/15/onedrive.aspx
```

Rules:
- Replace `.` and `-` in username portion with `_`
- Replace `.` in domain with `_`
- Lowercase everything

### Response Interpretation

| HTTP Status | Meaning |
|-------------|---------|
| `403` | Valid user (OneDrive provisioned) |
| `404` | User not found or OneDrive never accessed |
| `302` | Follow redirect, re-evaluate |
| Other | Log as unknown, skip |

### Concurrency Model

- Build a work queue of all usernames
- Spawn N worker threads where N = number of ACI proxy URLs provided
- Each worker thread owns one proxy URL exclusively (no sharing) — ensures IP diversity
- Workers pull from the queue until empty
- Falls back to single-threaded direct requests if no `--aci-urls` provided

### Tenant Derivation

- If `--tenant contoso.com` → use `contoso` as the SharePoint subdomain
- If `--tenant <UUID>` → require `--domain` to be set, derive subdomain from domain

## ACI Proxy for OneDrive Enum (`onedrive_proxy.py`)

New deployer script, separate from `aciproxy.py`, with SharePoint target baked in.

```bash
# Deploy 10 OneDrive enum proxies for contoso.com
python3 onedrive_proxy.py --tenant contoso.com --count 10 --outfile enum_urls.txt
```

Arguments:
- `--tenant` — target tenant/domain (determines SharePoint hostname)
- `--count` — number of ACI containers to deploy
- `--outfile` — output file for proxy URLs

## CLI Changes to `apimspray.py`

### Args Removed

- `--teams-urls`
- `--teams-region`
- `--sac-user`, `--sac-pass`, `--sac-accounts`
- `--no-presence`
- `--skip-sanity`

### Args Added

- `--aci-urls` — path to file of ACI proxy URLs for enumeration (optional)

### Unchanged Args (used by enumerate mode)

- `--users` — user list
- `--tenant` — tenant ID or domain
- `--domain` — domain suffix
- `--output` — output directory

## Code Removed from `apimspray.py`

- `TeamsEnumerator` class (~500 lines)
- `TeamsAPIMManager` class
- `TEAMS_ENUM_RATE_PER_TOKEN` constant
- Teams client headers, client ID, regions dict
- Sacrificial account authentication logic

## Output

Results written to existing output structure:
- `enumerated_<timestamp>.txt` — confirmed valid users (403 responses)

## Usage Example

```bash
# 1. Deploy OneDrive enum proxies
python3 onedrive_proxy.py --tenant contoso.com --count 10 --outfile enum_urls.txt

# 2. Enumerate users
python3 apimspray.py --mode enumerate --users users.txt --tenant contoso.com --aci-urls enum_urls.txt
```
