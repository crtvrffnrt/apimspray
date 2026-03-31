#!/usr/bin/env python3
"""
apimcreate.py - Unified Azure APIM Deployer (Bicep)
Deploys login and/or Teams APIM gateways in a single ARM deployment.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

# Colors
class Colors:
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    RESET = '\033[0m'

def log(level, message):
    colors = {"info": Colors.BLUE, "ok": Colors.GREEN, "warn": Colors.YELLOW, "error": Colors.RED}
    labels = {"info": "[INFO]", "ok": "[ OK ]", "warn": "[WARN]", "error": "[ERR ]"}
    print(f"{colors.get(level, '')}{labels.get(level, '')}{Colors.RESET} {message}")

def die(message):
    log("error", message)
    sys.exit(1)

def run_command(command, check=True):
    try:
        result = subprocess.run(command, check=check, shell=True, text=True, capture_output=True)
        return result.stdout.strip() if result.stdout else ""
    except subprocess.CalledProcessError as e:
        if check:
            raise
        return None

DEFAULT_RG_LOCATION = "germanywestcentral"

def get_az_regions():
    cmd = (
        "az provider show --namespace Microsoft.ApiManagement "
        "--query \"resourceTypes[?resourceType=='service'].locations[]\" "
        "-o tsv"
    )
    output = run_command(cmd)
    if not output:
        return []
    return [r.strip().replace(" ", "").lower() for r in output.split('\n') if r.strip()]

def normalize_location(value):
    return value.strip().replace(" ", "").lower()

def parse_location_list(value):
    if value is None:
        return []
    return [normalize_location(part) for part in value.split(",") if part.strip()]

def main():
    parser = argparse.ArgumentParser(
        description="apimcreate - Unified Azure APIM Deployer (Bicep)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--type", required=True, choices=["login", "teams", "both"],
        help="Type of APIM gateways to deploy:\n"
             " - login: proxies login.microsoftonline.com (for spray/validate)\n"
             " - teams: proxies teams.microsoft.com/api/mt (for enumerate)\n"
             " - both:  deploys login and teams gateways in one deployment",
    )
    parser.add_argument("--count", type=int, help="Number of instances per type")
    parser.add_argument("--outfile", help="Output file for login gateway URLs")
    parser.add_argument("--teams-outfile", help="Output file for Teams gateway URLs (required with --type both)")
    parser.add_argument(
        "--location",
        help="Comma-separated APIM location(s). First location used for resource group.",
    )
    parser.add_argument("--prefix", default=None, help="API URL prefix (default: oauth for login, teamsmt for teams)")
    parser.add_argument("--delete-old", action="store_true", help="Delete old resource groups before deploying")
    parser.add_argument("--delete-only", action="store_true", help="Only delete old resource groups")

    args = parser.parse_args()

    # Validate az login
    try:
        run_command("az account show")
    except Exception:
        die("Azure CLI not logged in. Run: az login")

    # Handle deletion
    if args.delete_only or args.delete_old:
        _delete_old_groups()
        if args.delete_only:
            return

    # Validate args for deployment
    deploy_login = args.type in ("login", "both")
    deploy_teams = args.type in ("teams", "both")

    if deploy_login and not args.outfile:
        die("--outfile is required for login gateway deployment")
    if deploy_teams and args.type == "both" and not args.teams_outfile:
        die("--teams-outfile is required with --type both")
    if deploy_teams and args.type == "teams" and not args.outfile and not args.teams_outfile:
        die("--outfile or --teams-outfile is required")

    # For --type teams with --outfile, treat outfile as teams output
    teams_outfile = args.teams_outfile or (args.outfile if args.type == "teams" else None)
    login_outfile = args.outfile if deploy_login else None

    # Discover regions
    log("info", "Fetching available APIM regions...")
    available_regions = get_az_regions()
    if not available_regions:
        die("No APIM regions found")
    log("ok", f"Discovered {len(available_regions)} regions")

    target_regions = available_regions
    rg_location = DEFAULT_RG_LOCATION

    if args.location:
        requested_regions = parse_location_list(args.location)
        if not requested_regions:
            die("No valid locations provided.")
        invalid = [loc for loc in requested_regions if loc not in available_regions]
        if invalid:
            die(f"Unknown APIM region(s): {', '.join(invalid)}")
        target_regions = requested_regions
        rg_location = requested_regions[0]
        log("info", f"Using requested APIM locations: {', '.join(target_regions)}")

    count = args.count
    if not count:
        if args.location:
            count = len(target_regions)
        else:
            die("--count is required when --location is not specified")
    if count < 1:
        die("Count must be at least 1")
    if count > len(target_regions):
        log("warn", f"Requested count {count} exceeds regions {len(target_regions)}. Locations will repeat.")

    login_prefix = args.prefix or "oauth"
    teams_prefix = args.prefix or "teamsmt"

    # Build instance lists
    login_instances = []
    teams_instances = []
    timestamp = int(time.time())

    if deploy_login:
        for i in range(count):
            region = target_regions[i % len(target_regions)]
            login_instances.append({"index": i, "region": region})
        log("info", f"Will deploy {count} login APIM instance(s)")

    if deploy_teams:
        for i in range(count):
            region = target_regions[i % len(target_regions)]
            teams_instances.append({"index": i, "region": region})
        log("info", f"Will deploy {count} Teams APIM instance(s)")

    total = len(login_instances) + len(teams_instances)
    log("info", f"Total APIM instances: {total}")

    # Create resource group
    resource_group = f"apim-deploy-{timestamp}"
    log("info", f"Creating Resource Group: {resource_group}")
    run_command(f"az group create --name {resource_group} --location {rg_location} --tags createdBy=apimcreate")
    log("ok", "Resource Group Ready")

    # Generate and deploy Bicep
    bicep_content = generate_bicep(login_instances, teams_instances, timestamp, login_prefix, teams_prefix)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.bicep', delete=False) as f:
        f.write(bicep_content)
        bicep_path = f.name

    deploy_name = f"apimcreate-{timestamp}"
    try:
        log("info", f"Deploying {total} APIM instance(s) via Bicep...")
        run_command(
            f"az deployment group create "
            f"--resource-group {resource_group} "
            f"--template-file {bicep_path} "
            f"--name {deploy_name} "
            f"--no-wait"
        )
        log("ok", "Deployment submitted — polling for progress...")
        _poll_deployment(resource_group, deploy_name, total)
    except subprocess.CalledProcessError as e:
        die(f"Deployment failed: {e}")
    finally:
        os.unlink(bicep_path)

    # Extract gateway URLs from deployed resources
    login_urls, teams_urls = extract_gateway_urls(resource_group, timestamp, login_instances, teams_instances, login_prefix, teams_prefix)

    # Write output files
    if login_outfile and login_urls:
        with open(login_outfile, 'w') as f:
            for url in login_urls:
                f.write(f"{url}\n")
        log("ok", f"Login URLs written to {login_outfile}")

    if teams_outfile and teams_urls:
        with open(teams_outfile, 'w') as f:
            for url in teams_urls:
                f.write(f"{url}\n")
        log("ok", f"Teams URLs written to {teams_outfile}")

    # Summary
    print("-" * 40)
    print(f"Resource Group : {resource_group}")
    if login_urls:
        print(f"Login Gateways : {len(login_urls)}")
    if teams_urls:
        print(f"Teams Gateways : {len(teams_urls)}")
    print(f"Total          : {len(login_urls) + len(teams_urls)}")
    print("-" * 40)


def generate_bicep(login_instances, teams_instances, timestamp, login_prefix, teams_prefix):
    """Generate a Bicep template with all APIM instances and their child resources."""
    lines = []

    # Login instances
    for inst in login_instances:
        idx = inst["index"]
        region = inst["region"]
        name = f"apimspray-{timestamp}-{idx}"
        _emit_instance(lines, name, region, login_prefix,
                       backend_url="https://login.microsoftonline.com",
                       api_id="oauth", api_display="OAuth",
                       product_id="apimspray-product", product_name="apimspray",
                       operations=[{"id": "logon", "method": "POST",
                                    "url_template": "/common/oauth2/token",
                                    "display_name": "logon"}],
                       output_prefix="login")

    # Teams instances
    for inst in teams_instances:
        idx = inst["index"]
        region = inst["region"]
        name = f"apimteams-{timestamp}-{idx}"
        _emit_instance(lines, name, region, teams_prefix,
                       backend_url="https://teams.microsoft.com/api/mt",
                       api_id="teamsapi", api_display="TeamsAPI",
                       product_id="apimteams-product", product_name="apimteams",
                       operations=[{"id": "enumuser", "method": "GET",
                                    "url_template": "/*",
                                    "display_name": "Teams User Enum"}],
                       output_prefix="teams")

    return "\n".join(lines)


def _emit_instance(lines, name, region, prefix, backend_url, api_id, api_display,
                   product_id, product_name, operations, output_prefix):
    """Emit Bicep resource blocks for a single APIM instance + children."""
    # Bicep resource names must be valid identifiers — replace hyphens
    res_id = name.replace("-", "_")

    lines.append(f"resource {res_id} 'Microsoft.ApiManagement/service@2022-08-01' = {{")
    lines.append(f"  name: '{name}'")
    lines.append(f"  location: '{region}'")
    lines.append(f"  sku: {{")
    lines.append(f"    name: 'Consumption'")
    lines.append(f"    capacity: 0")
    lines.append(f"  }}")
    lines.append(f"  properties: {{")
    lines.append(f"    publisherEmail: 'proxy@example.com'")
    lines.append(f"    publisherName: 'Proxy'")
    lines.append(f"  }}")
    lines.append(f"}}")
    lines.append("")

    # API
    api_res = f"{res_id}_api"
    lines.append(f"resource {api_res} 'Microsoft.ApiManagement/service/apis@2022-08-01' = {{")
    lines.append(f"  parent: {res_id}")
    lines.append(f"  name: '{api_id}'")
    lines.append(f"  properties: {{")
    lines.append(f"    displayName: '{api_display}'")
    lines.append(f"    path: '{prefix}'")
    lines.append(f"    protocols: ['https']")
    lines.append(f"    serviceUrl: '{backend_url}'")
    lines.append(f"    subscriptionRequired: false")
    lines.append(f"    apiType: 'http'")
    lines.append(f"  }}")
    lines.append(f"}}")
    lines.append("")

    # Operations
    for op in operations:
        op_res = f"{res_id}_op_{op['id']}"
        lines.append(f"resource {op_res} 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {{")
        lines.append(f"  parent: {api_res}")
        lines.append(f"  name: '{op['id']}'")
        lines.append(f"  properties: {{")
        lines.append(f"    displayName: '{op['display_name']}'")
        lines.append(f"    method: '{op['method']}'")
        lines.append(f"    urlTemplate: '{op['url_template']}'")
        lines.append(f"  }}")
        lines.append(f"}}")
        lines.append("")

    # Product
    prod_res = f"{res_id}_product"
    lines.append(f"resource {prod_res} 'Microsoft.ApiManagement/service/products@2022-08-01' = {{")
    lines.append(f"  parent: {res_id}")
    lines.append(f"  name: '{product_id}'")
    lines.append(f"  properties: {{")
    lines.append(f"    displayName: '{product_name}'")
    lines.append(f"    subscriptionRequired: false")
    lines.append(f"    state: 'published'")
    lines.append(f"  }}")
    lines.append(f"}}")
    lines.append("")

    # Product-API link
    link_res = f"{res_id}_prodapi"
    lines.append(f"resource {link_res} 'Microsoft.ApiManagement/service/products/apis@2022-08-01' = {{")
    lines.append(f"  parent: {prod_res}")
    lines.append(f"  name: '{api_id}'")
    lines.append(f"}}")
    lines.append("")

    # Output
    lines.append(f"output {res_id}_url string = '${{{res_id}.properties.gatewayUrl}}/{prefix}/'")
    lines.append("")


def extract_gateway_urls(resource_group, timestamp, login_instances, teams_instances, login_prefix, teams_prefix):
    """Extract gateway URLs from deployment outputs."""
    login_urls = []
    teams_urls = []

    deploy_name = f"apimcreate-{timestamp}"

    try:
        output_json = run_command(
            f"az deployment group show "
            f"--resource-group {resource_group} "
            f"--name {deploy_name} "
            f"--query properties.outputs -o json"
        )
        if not output_json:
            log("warn", "No deployment outputs found — falling back to resource query")
            return _extract_urls_fallback(resource_group, timestamp, login_instances, teams_instances, login_prefix, teams_prefix)

        outputs = json.loads(output_json)
        for key, val in outputs.items():
            url = val.get("value", "")
            if not url:
                continue
            if key.startswith("apimspray_"):
                login_urls.append(url)
            elif key.startswith("apimteams_"):
                teams_urls.append(url)
    except Exception as e:
        log("warn", f"Failed to read deployment outputs: {e} — falling back to resource query")
        return _extract_urls_fallback(resource_group, timestamp, login_instances, teams_instances, login_prefix, teams_prefix)

    return login_urls, teams_urls


def _extract_urls_fallback(resource_group, timestamp, login_instances, teams_instances, login_prefix, teams_prefix):
    """Fallback: query each APIM instance individually for its gateway URL."""
    login_urls = []
    teams_urls = []

    for inst in login_instances:
        name = f"apimspray-{timestamp}-{inst['index']}"
        try:
            gw = run_command(
                f"az apim show --name {name} --resource-group {resource_group} "
                "--query gatewayUrl -o tsv"
            )
            if gw:
                login_urls.append(f"{gw}/{login_prefix}/")
                log("ok", f"[{name}] {gw}/{login_prefix}/")
        except Exception:
            log("error", f"[{name}] Failed to get gateway URL")

    for inst in teams_instances:
        name = f"apimteams-{timestamp}-{inst['index']}"
        try:
            gw = run_command(
                f"az apim show --name {name} --resource-group {resource_group} "
                "--query gatewayUrl -o tsv"
            )
            if gw:
                teams_urls.append(f"{gw}/{teams_prefix}/")
                log("ok", f"[{name}] {gw}/{teams_prefix}/")
        except Exception:
            log("error", f"[{name}] Failed to get gateway URL")

    return login_urls, teams_urls


def _poll_deployment(resource_group, deploy_name, total_instances, poll_interval=15, timeout=600):
    """Poll ARM deployment until terminal state or timeout, printing per-resource progress."""
    start = time.monotonic()

    while True:
        time.sleep(poll_interval)
        elapsed = int(time.monotonic() - start)

        # Check overall deployment state
        state = run_command(
            f"az deployment group show "
            f"--resource-group {resource_group} "
            f"--name {deploy_name} "
            f"--query properties.provisioningState -o tsv",
            check=False,
        )
        if not state:
            log("info", f"Waiting for deployment to register... ({elapsed}s)")
            continue

        # Query per-instance provisioning state (name + state)
        res_json = run_command(
            f"az resource list "
            f"--resource-group {resource_group} "
            f"--resource-type Microsoft.ApiManagement/service "
            f"--query \"[].{{name:name, state:provisioningState}}\" -o json",
            check=False,
        )

        succeeded = 0
        running = 0
        failed = 0
        stuck_names = []
        if res_json:
            try:
                resources = json.loads(res_json)
                for r in resources:
                    s = r.get("state", "")
                    if s == "Succeeded":
                        succeeded += 1
                    elif s in ("Failed", "Canceled"):
                        failed += 1
                    else:
                        running += 1
                        stuck_names.append(r.get("name", "?"))
            except (json.JSONDecodeError, TypeError):
                pass

        mins, secs = divmod(elapsed, 60)
        time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"

        if succeeded + failed > 0:
            parts = [f"{succeeded}/{total_instances} provisioned"]
            if running:
                parts.append(f"{running} in progress")
            if failed:
                parts.append(f"{failed} failed")
            log("info", f"{' | '.join(parts)} ({time_str})")
        else:
            log("info", f"Provisioning APIM instances... ({time_str})")

        if state in ("Succeeded", "Failed", "Canceled"):
            if state == "Succeeded":
                log("ok", f"Deployment complete ({time_str})")
            elif state == "Failed":
                log("error", f"Deployment failed after {time_str}")
                if failed:
                    log("error", f"{failed} instance(s) failed to provision")
            else:
                log("warn", f"Deployment canceled after {time_str}")
            return

        # Timeout — proceed with whatever succeeded
        if elapsed >= timeout and succeeded > 0:
            log("warn", f"Timeout after {time_str} — {running} instance(s) still provisioning: {', '.join(stuck_names)}")
            log("warn", f"Proceeding with {succeeded} successfully provisioned instance(s)")
            return


def _delete_old_groups():
    log("info", "Checking for old resource groups...")
    deleted = 0
    for prefix in ("apim-rotator-", "apim-teams-rotator-", "apim-deploy-"):
        try:
            old_groups = run_command(
                f"az group list --query \"[?starts_with(name, '{prefix}')].name\" -o tsv"
            )
            if old_groups:
                for grp in old_groups.split():
                    log("info", f"Deleting {grp}...")
                    run_command(f"az group delete --name {grp} --yes --no-wait")
                    deleted += 1
        except Exception:
            pass
    if deleted > 0:
        log("ok", f"Queued {deleted} resource group(s) for deletion")
    else:
        log("info", "No old resource groups found")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Interrupted")
