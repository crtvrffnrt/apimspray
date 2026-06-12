# Azure Container Instances (ACI) Proxy for Teams Enumeration — Design

## Problem

Azure Functions on Consumption plan share outbound NAT IPs across the platform, similar to APIM Consumption tier. Despite deploying 15 Function Apps across regions, the actual outbound IP diversity is insufficient, resulting in continued 429 rate limiting from Teams API.

Teams `externalsearchv3` rate-limits per source IP (~2-3 req/s per IP). We need containers with guaranteed unique public IPs.

## Solution

Deploy Azure Container Instances (ACI) as HTTP proxies. Each ACI container gets a dedicated public IP at creation — guaranteed unique, no NAT sharing. This provides real IP diversity.

## Architecture

```
apimspray → ACI container (eastus, IP: 20.x.x.1)    → teams.microsoft.com
          → ACI container (eastus, IP: 20.x.x.2)    → teams.microsoft.com
          → ACI container (westeurope, IP: 40.x.x.3) → teams.microsoft.com
          ...each container has its own dedicated public IP
```

- 15-30 ACI containers, each running a minimal Python HTTP proxy
- Each container gets a dedicated public IP — no shared NAT
- Multiple containers can be deployed in the same region, each with a unique IP
- Container image: Flask + gunicorn, ~20 lines of proxy code
- Image built via `az acr build` (no local Docker needed)
- URLs consumed by apimspray via existing `--teams-urls` mechanism
- Cost: ~$0.000013/s per container — pennies for a 30-minute enumeration run

## Deployer Script: `aciproxy.py`

### Commands

- `--deploy`: Create ACR, build+push image, deploy ACI containers
- `--destroy`: Delete all aciproxy resource groups
- `--delete-old`: Clean up old deployments before deploying new
- `--count N`: Number of containers (default 15)
- `--regions`: Comma-separated regions (default: eastus). Containers round-robin across specified regions
- `--outfile`: Write proxy URLs to file

### Deployment Flow

1. Create resource group
2. Create Azure Container Registry (ACR)
3. Build container image via `az acr build` (pushes to ACR)
4. Deploy N container instances, round-robin across specified regions
5. Each container gets a public IP on port 8080
6. Output URLs: `http://<container-ip>:8080/` — one per line

### Container Image

Minimal Flask app + gunicorn:
- Receives any HTTP request
- Forwards to `https://teams.microsoft.com/api/mt/<path>`
- Returns response transparently
- ~20 lines of Python code

### Region Distribution

- `--regions eastus --count 15` → 15 containers in eastus, 15 unique IPs
- `--regions eastus,westeurope --count 15` → 8 in eastus, 7 in westeurope
- Default: `--regions eastus --count 15`

## Integration with apimspray.py

No changes needed. ACI proxy URLs go into `--teams-urls` file. The auto-scaling rate calculation already handles it: `rate_per_ip × num_proxies / num_tokens`.

## Expected Performance

With 15 ACI containers (15 guaranteed unique IPs):
- High pace, 1 token: `2.5 × 15 = ~37 req/s` → 48k users in ~22 minutes
- High pace, 2 tokens: `2.5 × 15 / 2 = ~19 req/s/token` → ~38 total → ~21 minutes

## Cleanup

`aciproxy.py --destroy` deletes ACR and all container groups. Containers are billed per-second only while running.
