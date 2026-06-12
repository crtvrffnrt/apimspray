import sys
import os
import time
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apimcreate


def test_cleanup_deletes_expired_groups():
    """Groups older than TTL_SECONDS are deleted."""
    now = int(time.time())
    old_ts = now - 30000  # older than 28800
    fresh_ts = now - 100  # recent

    delete_calls = []
    def fake_run(cmd, check=True):
        if "group list" in cmd and "apim-deploy-" in cmd:
            return f"apim-deploy-{old_ts}\napim-deploy-{fresh_ts}"
        if "group list" in cmd:
            return ""
        if "group delete" in cmd:
            delete_calls.append(cmd)
        return ""

    with patch("apimcreate.run_command", side_effect=fake_run), \
         patch("apimcreate.time") as mock_time:
        mock_time.time.return_value = now
        mock_time.monotonic = time.monotonic
        apimcreate._cleanup_expired_groups()

    # Should delete only the old one
    assert len(delete_calls) == 1
    assert str(old_ts) in delete_calls[0]


def test_cleanup_skips_when_no_expired():
    """No deletions when all groups are fresh."""
    now = int(time.time())
    fresh_ts = now - 100

    delete_calls = []
    def fake_run(cmd, check=True):
        if "group list" in cmd and "apim-deploy-" in cmd:
            return f"apim-deploy-{fresh_ts}"
        if "group list" in cmd:
            return ""
        if "group delete" in cmd:
            delete_calls.append(cmd)
        return ""

    with patch("apimcreate.run_command", side_effect=fake_run), \
         patch("apimcreate.time") as mock_time:
        mock_time.time.return_value = now
        mock_time.monotonic = time.monotonic
        apimcreate._cleanup_expired_groups()

    assert len(delete_calls) == 0


def test_cleanup_handles_all_prefixes():
    """Checks all three APIM prefixes."""
    now = int(time.time())
    old_ts = now - 30000

    call_count = [0]
    def fake_run(cmd, check=True):
        if "group list" in cmd:
            if "apim-deploy-" in cmd:
                return f"apim-deploy-{old_ts}"
            elif "apim-rotator-" in cmd:
                return f"apim-rotator-{old_ts}"
            elif "apim-teams-rotator-" in cmd:
                return ""
        call_count[0] += 1
        return ""

    with patch("apimcreate.run_command", side_effect=fake_run), \
         patch("apimcreate.time") as mock_time:
        mock_time.time.return_value = now
        mock_time.monotonic = time.monotonic
        apimcreate._cleanup_expired_groups()

    # Two groups should have been deleted (apim-deploy + apim-rotator)
    assert call_count[0] == 2


def test_cleanup_ignores_unparseable_names():
    """Groups without a valid timestamp in the name are skipped."""
    delete_calls = []
    def fake_run(cmd, check=True):
        if "group list" in cmd and "apim-deploy-" in cmd:
            return "apim-deploy-notanumber"
        if "group list" in cmd:
            return ""
        if "group delete" in cmd:
            delete_calls.append(cmd)
        return ""

    with patch("apimcreate.run_command", side_effect=fake_run), \
         patch("apimcreate.time") as mock_time:
        mock_time.time.return_value = int(time.time())
        mock_time.monotonic = time.monotonic
        apimcreate._cleanup_expired_groups()

    assert len(delete_calls) == 0
