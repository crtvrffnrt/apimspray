# Azure Functions HTTP Proxy for Teams Enumeration — Design

## Problem

Teams `externalsearchv3` rate-limits per source IP (~2-3 req/s per IP), not per token. APIM Consumption tier shares outbound IPs across instances, so 15 APIM gateways yield only 1-3 actual outbound IPs. No code-level rate tuning can overcome this — we need real IP diversity.

TeamFiltration achieved ~27+ req/s with a single token by routing through AWS API Gateway, which assigned a unique IP per request. AWS now bans this use case, so we need an Azure-native equivalent.

## Goal

Deploy Azure Functions across 10-15 regions as HTTP proxies to Teams, giving each region its own outbound IP. This provides 15+ unique IPs, enabling 30-45 req/s aggregate throughput and ~27-minute enumeration of 48k users with a single sac account.

## Architecture

```
apimspray → Azure Function (eastus)    → teams.microsoft.com
         → Azure Function (westeurope) → teams.microsoft.com
         → Azure Function (japaneast)  → teams.microsoft.com
         ...round-robin per request
```

- 10-15 Function Apps, one per Azure region, each on Consumption plan
- Each Function App gets its own outbound IP addresses
- Minimal Python HTTP trigger (~15 lines) that proxies requests to `https://teams.microsoft.com/api/mt/`
- URLs passed to apimspray via existing `--teams-urls` mechanism
- Cost: Free (Azure Functions Consumption plan includes 1M free executions/month)

## Deployer Script: `funcproxy.py`

Follows the same pattern as `apimcreate.py` — CLI tool to deploy/teardown Azure resources.

### Deploy

- Creates a resource group + Function App per region using Azure CLI (`az functionapp create` with Consumption plan)
- Function code: single `__init__.py` HTTP trigger that receives path/headers/body, forwards to Teams, returns response
- Storage account created automatically by `az functionapp create`
- Default 15 regions: eastus, westeurope, japaneast, australiaeast, southeastasia, northeurope, westus2, centralindia, brazilsouth, canadacentral, uksouth, koreacentral, francecentral, switzerlandnorth, norwayeast
- Configurable via `--regions`
- Outputs Function URLs, one per line, ready for `--teams-urls`

### Teardown

- `--destroy` flag deletes all resource groups
- Function apps on Consumption plan cost nothing when idle, so teardown is optional

### Key differences from `apimcreate.py`

- No Bicep templates — uses `az` CLI + zip deploy
- Much simpler — no subscription keys, no API definitions
- Each Function App contains ~15 lines of Python proxy code

## Integration with apimspray.py

Minimal changes:

- Function URLs look like `https://funcproxy-eastus.azurewebsites.net/api/proxy`
- Passed via existing `--teams-urls url1,url2,...`
- `_build_enum_url` already round-robins across URLs — no change needed
- Proxy is transparent: apimspray sends the same requests it would send to APIM

## Rate Settings with IP Diversity

With 15 Functions (15 unique IPs), each tolerating ~2-3 req/s:

| Pace    | Rate (req/s) | Threads/Token |
|---------|-------------|---------------|
| high    | 30          | 15            |
| medium  | 20          | 10            |
| low     | 10          | 5             |
| stealth | 3           | 3             |

At high pace: 48,000 / 30 = ~27 minutes — matching TeamFiltration performance.

### Simplified 429 handling

With real IP diversity, 429s become rare. On a 429, simple sleep for `Retry-After` seconds — no adaptive backoff spiral, no re-queue complexity.

## Cleanup

`funcproxy.py --destroy` deletes all resource groups. Teardown is optional since Consumption plan has zero idle cost.
