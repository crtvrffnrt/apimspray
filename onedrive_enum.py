#!/usr/bin/env python3
"""
onedrive_enum.py - OneDrive-based user enumeration for apimspray.

Checks whether users have OneDrive provisioned by making GET requests to:
  https://[tenant]-my.sharepoint.com/personal/[user]_[domain]_com/_layouts/15/onedrive.aspx

Response interpretation:
  403 Forbidden  -> valid user (OneDrive provisioned)
  404 Not Found  -> user not found or OneDrive never accessed
  302 redirect   -> followed transparently by allow_redirects=True; final status evaluated
  other          -> unknown / error
"""

import itertools
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")

def _c(text, *codes):
    if not _USE_COLOR:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"

_SAFE_RE = re.compile(r'[.\-]')


def build_onedrive_path(upn):
    """
    Build the OneDrive personal URL path for a given UPN.

    john.doe@contoso.com -> personal/john_doe_contoso_com/_layouts/15/onedrive.aspx
    """
    upn = upn.lower()
    username, domain = upn.split("@", 1)
    username_safe = _SAFE_RE.sub("_", username)
    domain_safe = domain.replace(".", "_")
    return f"personal/{username_safe}_{domain_safe}/_layouts/15/onedrive.aspx"


class OneDriveEnumerator:
    """
    Enumerates valid users via OneDrive URL probing.

    Uses a thread pool with one thread per proxy URL.
    Falls back to a single direct thread when no proxies are provided.
    """

    def __init__(self, proxy_urls=None, threads=100, timeout=5, retries=1, debug=False):
        self.proxy_urls = list(proxy_urls) if proxy_urls else []
        self.threads = threads
        self.timeout = timeout
        self.retries = retries
        self.debug = debug

    def _check_user(self, upn, tenant_name=None, proxy_url=None, session=None):
        """
        Make a single OneDrive check for a UPN.

        Returns: 'valid', 'not_found', or 'error'
        """
        path = build_onedrive_path(upn)
        if proxy_url:
            url = f"{proxy_url.rstrip('/')}/{path}"
        else:
            url = f"https://{tenant_name}-my.sharepoint.com/{path}"

        requester = session or requests
        try:
            resp = requester.get(url, timeout=self.timeout, allow_redirects=False)
            if resp.status_code in (302, 403):
                return "valid"
            elif resp.status_code == 404:
                return "not_found"
            else:
                if self.debug:
                    print(f"{_c('[ERROR]', '1', '31')} unexpected status {_c(str(resp.status_code), '31')} for {upn} via {proxy_url}")
                return "error"
        except requests.RequestException as e:
            if self.debug:
                print(f"{_c('[ERROR]', '1', '31')} {upn}: {type(e).__name__}: {_c(str(e), '31')}")
            return "error"

    def enumerate(self, users, tenant_name=None, logger=None):
        """
        Enumerate all users using the thread pool, round-robining across proxies.

        Thread count is independent of proxy count — many threads share the proxy pool.
        Returns: (valid_users: set, counters: dict)
        """
        user_queue = queue.Queue()
        for u in users:
            user_queue.put(u)

        proxy_pool = self.proxy_urls if self.proxy_urls else [None]
        n_threads = self.threads

        valid_users = set()
        results_lock = threading.Lock()
        counters = {"completed": 0, "valid": 0, "not_found": 0, "errors": 0}

        total = len(users)
        start_time = time.time()
        stop_progress = threading.Event()

        ERROR_WARN_THRESHOLD = 0.20  # warn if >20% of completed are errors
        error_warned = [False]

        def progress_printer():
            while not stop_progress.wait(10):
                with results_lock:
                    done = counters["completed"]
                    found = counters["valid"]
                    errors = counters["errors"]
                elapsed = int(time.time() - start_time)
                rate = done / elapsed if elapsed > 0 else 0
                remaining = total - done
                eta = int(remaining / rate) if rate > 0 else 0
                eta_str = f"{eta//60}m{eta%60:02d}s" if eta > 0 else "?"
                err_str = f" | Errors: {_c(str(errors), '1', '31')}" if errors else ""
                print(
                    f"{_c('[*]', '1', '36')} Progress: "
                    f"{_c(str(done), '1')}/{_c(str(total), '1')} | "
                    f"Found: {_c(str(found), '1', '32')} | "
                    f"{_c(f'{rate:.1f} req/s', '35')} | "
                    f"ETA: {_c(eta_str, '33')}"
                    f"{err_str}"
                )
                # Warn once if error rate is excessive
                if not error_warned[0] and done >= 50 and errors / done > ERROR_WARN_THRESHOLD:
                    error_warned[0] = True
                    print(
                        f"{_c('[!]', '1', '33')} High error rate ({errors}/{done} = {errors/done:.0%}) — "
                        f"likely proxy timeouts. Try --timeout {self.timeout + 10} or fewer --threads."
                    )

        progress_thread = threading.Thread(target=progress_printer, daemon=True)
        progress_thread.start()

        # One persistent session per proxy URL — shared across threads for connection reuse
        sessions = {p: requests.Session() for p in proxy_pool}

        # Shared round-robin proxy iterator
        proxy_iter = itertools.cycle(proxy_pool)
        proxy_iter_lock = threading.Lock()

        def next_proxy():
            with proxy_iter_lock:
                return next(proxy_iter)

        def run_pass(upn_list):
            q = queue.Queue()
            for u in upn_list:
                q.put(u)
            errored = []
            errored_lock = threading.Lock()

            def worker():
                while True:
                    try:
                        upn = q.get_nowait()
                    except queue.Empty:
                        break
                    proxy_url = next_proxy()
                    try:
                        result = self._check_user(upn, tenant_name, proxy_url, session=sessions[proxy_url])
                        if self.debug:
                            color = '32' if result == 'valid' else '31' if result == 'error' else '2'
                            print(f"{_c('[DEBUG]', '2', '33')} {upn} -> {_c(result, color)} (proxy: {proxy_url})")
                        with results_lock:
                            counters["completed"] += 1
                            if result == "valid":
                                counters["valid"] += 1
                                valid_users.add(upn)
                                print(f"{_c('[+]', '1', '32')} {_c('VALID:', '1', '32')} {_c(upn, '32')}")
                                if logger:
                                    logger.log_result("enumerated", upn)
                            elif result == "not_found":
                                counters["not_found"] += 1
                            else:
                                counters["errors"] += 1
                                with errored_lock:
                                    errored.append(upn)
                    finally:
                        q.task_done()

            with ThreadPoolExecutor(max_workers=n_threads) as executor:
                futures = [executor.submit(worker) for _ in range(n_threads)]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        pass

            return errored

        # Main pass
        pending = list(users)
        pending = run_pass(pending)

        # Retry passes
        for attempt in range(1, self.retries + 1):
            if not pending:
                break
            print(f"{_c('[*]', '1', '36')} Retrying {_c(str(len(pending)), '1', '33')} errored users (attempt {attempt}/{self.retries})...")
            counters["errors"] -= len(pending)  # will be re-counted in retry
            counters["completed"] -= len(pending)
            pending = run_pass(pending)

        stop_progress.set()
        progress_thread.join(timeout=2)
        for s in sessions.values():
            s.close()
        return valid_users, counters
