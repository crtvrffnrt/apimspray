#!/usr/bin/env python3
"""
onedrive_proxy.py - Deploy Azure Container Instances as HTTP proxies for OneDrive user enumeration.
Each container gets a unique public Azure IP, enabling parallel enumeration across multiple IPs.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import requests


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


DEFAULT_REGIONS = [
    "eastus", "eastus2", "westus", "westus2", "westus3",
    "centralus", "northcentralus", "southcentralus", "westcentralus",
]
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


def verify_tenant(sharepoint_host):
    """
    Check that the SharePoint hostname exists and responds like a real tenant.

    A valid tenant returns 200, 302, or 403.
    A non-existent tenant typically returns 404 or redirects to a Microsoft error page.
    Returns (ok: bool, status_code: int or None)
    """
    url = f"https://{sharepoint_host}/_layouts/15/onedrive.aspx"
    try:
        resp = requests.get(url, timeout=10, allow_redirects=True)
        return resp.status_code in (200, 302, 403), resp.status_code
    except requests.RequestException as e:
        return False, None


def _check_sharepoint_name(name):
    """Test if a given name resolves as a SharePoint tenant. Returns the host if valid, else None."""
    host = f"{name}-my.sharepoint.com"
    ok, status = verify_tenant(host)
    return host if ok else None


def discover_sharepoint_host(domain):
    """
    Auto-discover the SharePoint tenant hostname for a given email domain.

    Tries multiple discovery methods:
      1. Simple domain prefix (contoso.com -> contoso-my.sharepoint.com)
      2. GetUserRealm API — returns FederationBrandName which is often the tenant name
      3. OIDC endpoint — confirms the domain is Azure AD and gets the tenant GUID
      4. DNS MX records — *.mail.protection.outlook.com pattern reveals tenant prefix
      5. Common variations (domaintld, domain-tld, etc.)

    Returns (sharepoint_host, method_used) or (None, None) if all methods fail.
    """
    base = domain.split(".")[0]
    tld = domain.split(".")[-1] if "." in domain else ""

    # Method 1: Simple prefix (most common case)
    log("info", f"Trying {base}-my.sharepoint.com ...")
    host = _check_sharepoint_name(base)
    if host:
        return host, "domain prefix"

    # Method 2: GetUserRealm API
    log("info", "Querying GetUserRealm API for tenant info...")
    try:
        realm_resp = requests.get(
            f"https://login.microsoftonline.com/getuserrealm.srf?login=user@{domain}&xml=0",
            timeout=10
        )
        if realm_resp.status_code == 200:
            realm = realm_resp.json()
            ns_type = realm.get("NameSpaceType")
            brand = realm.get("FederationBrandName", "")
            cloud_instance = realm.get("CloudInstanceName", "")

            if ns_type == "Unknown":
                log("warn", f"{domain} is not a valid Microsoft 365 domain (NameSpaceType: Unknown)")
                return None, None

            log("info", f"NameSpaceType: {ns_type}, FederationBrandName: {brand}")

            # Try the brand name as tenant (clean it for SharePoint subdomain format)
            if brand:
                brand_clean = re.sub(r'[^a-zA-Z0-9]', '', brand).lower()
                if brand_clean and brand_clean != base:
                    log("info", f"Trying brand name: {brand_clean}-my.sharepoint.com ...")
                    host = _check_sharepoint_name(brand_clean)
                    if host:
                        return host, f"FederationBrandName ({brand})"
    except requests.RequestException:
        pass

    # Method 3: DKIM CNAME records — very reliable when present
    # selector1._domainkey.domain.com CNAME -> selector1-domain-com._domainkey.{tenant}.onmicrosoft.com
    log("info", "Checking DKIM CNAME records...")
    for selector in ("selector1", "selector2"):
        try:
            dkim_out = subprocess.run(
                ["dig", "+short", "CNAME", f"{selector}._domainkey.{domain}"],
                capture_output=True, text=True, timeout=10
            )
            if dkim_out.returncode == 0 and dkim_out.stdout.strip():
                cname = dkim_out.stdout.strip().rstrip(".")
                # Pattern: selector1-domain-tld._domainkey.TENANTNAME.onmicrosoft.com
                m = re.search(r'\._domainkey\.([^.]+)\.onmicrosoft\.com$', cname)
                if m:
                    dkim_tenant = m.group(1).lower()
                    log("info", f"DKIM reveals tenant: {dkim_tenant}")
                    host = _check_sharepoint_name(dkim_tenant)
                    if host:
                        return host, f"DKIM CNAME ({cname})"
        except Exception:
            pass

    # Method 4: OIDC endpoint to confirm Azure AD + get tenant ID
    log("info", "Querying OpenID configuration...")
    tenant_id = None
    try:
        oidc_resp = requests.get(
            f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration",
            timeout=10
        )
        if oidc_resp.status_code == 200:
            oidc = oidc_resp.json()
            issuer = oidc.get("issuer", "")
            # Extract tenant GUID from issuer URL
            m = re.search(r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/', issuer)
            if m:
                tenant_id = m.group(1)
                log("info", f"Tenant ID: {tenant_id}")
        else:
            log("warn", f"OIDC lookup failed (HTTP {oidc_resp.status_code}) — domain may not be Azure AD")
    except requests.RequestException:
        pass

    # Method 5: DNS MX record — look for *.mail.protection.outlook.com
    log("info", "Checking DNS MX records...")
    try:
        mx_out = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=10
        )
        if mx_out.returncode == 0 and mx_out.stdout:
            for line in mx_out.stdout.strip().split("\n"):
                parts = line.strip().split()
                if len(parts) >= 2:
                    mx_host = parts[-1].rstrip(".")
                    if "mail.protection.outlook.com" in mx_host:
                        mx_prefix = mx_host.split(".mail.protection.outlook.com")[0]
                        mx_prefix = mx_prefix.rstrip("-")
                        if mx_prefix and mx_prefix != base:
                            log("info", f"MX record suggests tenant: {mx_prefix}")
                            host = _check_sharepoint_name(mx_prefix)
                            if host:
                                return host, f"DNS MX ({mx_host})"
    except Exception:
        pass

    # Method 6: Brand name abbreviations
    # FederationBrandName like "New England Donor Services, Inc." -> try abbreviations
    # Orgs often use creative abbreviations: first letters of some words + full remaining words
    brand_candidates = []
    if brand:
        words = re.sub(r'[^a-zA-Z0-9\s]', '', brand).split()
        words_lower = [w.lower() for w in words if w.lower() not in
                       ("inc", "llc", "corp", "co", "ltd", "the", "of", "and", "for")]
        if len(words_lower) >= 2:
            # Acronym from first letters: "New England Donor Services" -> "neds"
            acronym = "".join(w[0] for w in words_lower)
            brand_candidates.append(acronym)
            # First N words joined: newengland, newenglanddonor
            for i in range(2, len(words_lower)):
                brand_candidates.append("".join(words_lower[:i]))
            # Abbreviate first K words to first N letters + remaining full words
            # e.g. "New England Donor Services" -> ne+donorservices, n+englanddonorservices
            for abbrev_count in range(1, min(len(words_lower), 4)):
                remaining = "".join(words_lower[abbrev_count:])
                if not remaining:
                    continue
                for letter_count in (1, 2, 3):
                    prefix = "".join(w[:letter_count] for w in words_lower[:abbrev_count])
                    brand_candidates.append(prefix + remaining)

    # Method 7: Common domain variations + brand abbreviations
    candidates = list(brand_candidates)
    if tld:
        candidates.append(f"{base}{tld}")           # nedsorg
        candidates.append(f"{base}-{tld}")           # neds-org
    # Try with "the" prefix stripped or added
    if base.startswith("the"):
        candidates.append(base[3:])
    else:
        candidates.append(f"the{base}")
    # Try with common suffixes
    for suffix in ("inc", "corp", "co", "llc", "org", "foundation", "edu", "online"):
        candidates.append(f"{base}{suffix}")

    # Deduplicate, skip already-tried names
    seen = {base}
    if brand:
        seen.add(re.sub(r'[^a-zA-Z0-9]', '', brand).lower())  # full brand already tried in Method 2
    for candidate in candidates:
        candidate = re.sub(r'[^a-zA-Z0-9]', '', candidate).lower()
        if candidate in seen or not candidate:
            continue
        seen.add(candidate)
        log("info", f"Trying variation: {candidate}-my.sharepoint.com ...")
        host = _check_sharepoint_name(candidate)
        if host:
            return host, f"name variation ({candidate})"

    return None, None


def deploy(tenant, domain, regions, count, outfile):
    """Deploy ACI containers as OneDrive enum proxies."""
    sharepoint_host = derive_sharepoint_host(tenant, domain)

    # Verify tenant exists before spending time on Azure resources
    log("info", f"Verifying tenant: {sharepoint_host}...")
    ok, status = verify_tenant(sharepoint_host)
    if not ok:
        # Simple derivation failed — try auto-discovery
        discover_domain = domain or tenant
        if "." in discover_domain:
            log("warn", f"{sharepoint_host} returned HTTP {status or 'no response'}. "
                f"Attempting auto-discovery for {discover_domain}...")
            discovered_host, method = discover_sharepoint_host(discover_domain)
            if discovered_host:
                log("ok", f"Discovered SharePoint host: {discovered_host} (via {method})")
                sharepoint_host = discovered_host
            else:
                die(f"Tenant verification failed for {sharepoint_host} and auto-discovery "
                    f"could not find a valid SharePoint host for {discover_domain}. "
                    f"Use --tenant with the correct SharePoint subdomain.")
        else:
            status_str = str(status) if status else "no response"
            die(f"Tenant verification failed for {sharepoint_host} (HTTP {status_str}). "
                f"Check that the tenant exists and has SharePoint/OneDrive enabled.")
    timestamp = int(time.time())
    rg_name = f"{RG_PREFIX}{timestamp}"
    acr_name = f"{ACR_PREFIX}{timestamp}"
    rg_location = regions[0]

    try:
        run_command("az account show")
    except Exception:
        die("Azure CLI not logged in. Run: az login")

    for ns in ("Microsoft.ContainerInstance", "Microsoft.ContainerRegistry"):
        reg_state = run_command(
            f"az provider show --namespace {ns} "
            f"--query registrationState -o tsv", check=False)
        if reg_state and reg_state.strip() != "Registered":
            log("info", f"Registering {ns} provider...")
            run_command(f"az provider register --namespace {ns}")
            for _ in range(30):
                time.sleep(5)
                state = run_command(
                    f"az provider show --namespace {ns} "
                    f"--query registrationState -o tsv", check=False)
                if state and state.strip() == "Registered":
                    break
            else:
                die(f"{ns} provider did not register in time.")
            log("ok", f"{ns} provider registered")

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
                f"--restart-policy Never",
                check=False
            )
            # Always check for IP — az container create can return non-zero even on success
            ip = run_command(
                f"az container show --resource-group {rg_name} "
                f"--name {container_name} "
                f"--query ipAddress.ip -o tsv",
                check=False
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
                    log("error", f"{tag}: No IP assigned — quota likely exceeded in {region} ({completed[0]}/{count})")
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
    parser.add_argument("--discover", action="store_true",
                        help="Auto-discover the SharePoint tenant name for a domain")
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

    if args.discover:
        if not args.tenant:
            die("--tenant is required for discovery (e.g. --discover --tenant neds.org)")
        domain = args.tenant
        log("info", f"Auto-discovering SharePoint tenant for: {domain}")
        # First try simple derivation
        simple_host = derive_sharepoint_host(domain)
        ok, status = verify_tenant(simple_host)
        if ok:
            log("ok", f"SharePoint host: {simple_host}")
            return
        log("warn", f"{simple_host} returned HTTP {status or 'no response'}")
        host, method = discover_sharepoint_host(domain)
        if host:
            log("ok", f"Discovered: {host} (via {method})")
            tenant_name = host.split("-my.")[0]
            print(f"\nUse this for enumeration:")
            print(f"  python3 apimspray.py --mode enumerate --tenant {tenant_name} --users users.txt")
        else:
            die(f"Could not discover SharePoint tenant for {domain}")
        return

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
