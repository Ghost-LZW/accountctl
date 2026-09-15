"""Explicit opt-in for a real legacy fingerprint/proxy, never its login data."""

import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from account_workspaces.fingerprint import browser_options, prepare_fingerprint
from account_workspaces.network import parse_proxy
from account_workspaces.storage import atomic_write

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not os.getenv("ACCOUNT_WORKSPACES_LEGACY_TEST_PROFILE"),
        reason="explicit legacy profile required; only fingerprint is copied",
    ),
]

STABLE_PROBE = """() => ({
  ua:navigator.userAgent, platform:navigator.platform,
  languages:navigator.languages, cores:navigator.hardwareConcurrency,
  screen:[screen.width,screen.height,screen.availWidth,screen.availHeight],
  window:[innerWidth,innerHeight,outerWidth,outerHeight],
  timezone:Intl.DateTimeFormat().resolvedOptions().timeZone
})"""


@pytest.mark.parametrize("headless", [True, False])
def test_original_script_launch_matches_managed_legacy(catalog, headless):
    from camoufox.sync_api import Camoufox
    from playwright.sync_api import sync_playwright

    source = Path(os.environ["ACCOUNT_WORKSPACES_LEGACY_TEST_PROFILE"]).resolve()
    original_fp = (source / "fingerprint.json").read_bytes()
    original_meta = (source / "meta.json").read_bytes()
    metadata = json.loads(original_meta)
    width, height = map(int, metadata["window"].split("x"))
    account = catalog.accounts[0]
    account = replace(
        account,
        browser=replace(
            account.browser,
            window=(width, height),
            legacy=True,
            geoip=metadata["geoip"],
        ),
    )
    atomic_write(account.profile / "fingerprint.json", original_fp.decode())
    # A separate disposable baseline lets the ORIGINAL script build its actual options.
    baseline = catalog.path.parent / "baseline"
    atomic_write(baseline / "fingerprint.json", original_fp.decode())
    # Use argv only inside this process; credentials never become OS process arguments.
    script = Path(__file__).parents[1] / "camoufox_browser.py"
    spec = importlib.util.spec_from_file_location("legacy_browser", script)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    captured = {}

    class Captured(BaseException):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise Captured

    argv = [
        "camoufox_browser.py",
        "-r",
        str(baseline),
        "-w",
        metadata["window"],
    ]
    if headless:
        argv.append("--headless")
    if metadata.get("proxy"):
        argv += ["-p", metadata["proxy"]]
    if metadata.get("geoip"):
        argv += ["--geoip"]
    with patch.object(old, "Camoufox", capture), patch.object(sys, "argv", argv):
        with pytest.raises(Captured):
            old.main()
    proxy = parse_proxy(metadata["proxy"]) if metadata.get("proxy") else None
    # Avoid assertion introspection displaying credential dictionaries on failure.
    if captured.get("proxy") != proxy:
        pytest.fail("legacy proxy settings differ", pytrace=False)
    try:
        with Camoufox(**captured) as context:
            before = context.pages[0].evaluate(STABLE_PROBE)
        first = prepare_fingerprint(account, proxy=proxy)
        second = prepare_fingerprint(account, proxy=proxy)
        with sync_playwright() as playwright:
            options = browser_options(account, first, headless=headless, proxy=proxy)
            with playwright.firefox.launch_persistent_context(**options) as context:
                page = context.pages[0]
                after = page.evaluate(STABLE_PROBE)
                # One read-only public request verifies the authenticated proxy in the browser.
                response = page.goto("https://example.com/", timeout=30_000)
                reachable = response is not None and response.ok
    except Exception:
        # Library exceptions may contain proxy auth, so never include their text/traceback.
        pytest.fail("legacy browser/proxy integration failed (details redacted)", pytrace=False)
    assert before == after
    assert first == second
    assert reachable
    assert (source / "fingerprint.json").read_bytes() == original_fp
    assert (source / "meta.json").read_bytes() == original_meta
    assert not (source / "workspace.json").exists()
