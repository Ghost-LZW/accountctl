"""Opt-in real browser evidence, entirely local and using throwaway accounts."""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from account_workspaces.config import load_catalog
from account_workspaces.fingerprint import browser_options, prepare_fingerprint
from account_workspaces.lifecycle import session_status, start_account, stop_account
from account_workspaces.storage import lock

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("ACCOUNT_WORKSPACES_BROWSER_TESTS") != "1",
        reason="set ACCOUNT_WORKSPACES_BROWSER_TESTS=1 for real Camoufox",
    ),
]

PROBE = """() => {
  const c = document.createElement('canvas'); c.width=200; c.height=50;
  const ctx=c.getContext('2d'); ctx.fillStyle='#f60'; ctx.fillRect(0,0,200,50);
  ctx.font='17px Arial'; ctx.fillStyle='#123'; ctx.fillText('Stable fingerprint',2,25);
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {
    ua:navigator.userAgent, platform:navigator.platform, languages:navigator.languages,
    cores:navigator.hardwareConcurrency, canvas:c.toDataURL(),
    screen:[screen.width,screen.height,screen.colorDepth],
    timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
    renderer:ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null
  };
}"""


@pytest.mark.parametrize("headless", [True, False])
def test_real_restart_stability_and_account_isolation(catalog, headless):
    from playwright.sync_api import sync_playwright

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><title>local test</title></html>")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    snapshots = []
    try:
        with sync_playwright() as playwright:
            for index in (0, 1, 0):
                account = catalog.accounts[index]
                with lock(account.profile / ".accountctl.lock"):
                    manifest = prepare_fingerprint(account)
                    options = browser_options(account, manifest, headless=headless, proxy=None)
                    with playwright.firefox.launch_persistent_context(**options) as context:
                        page = context.pages[0]
                        page.goto(url)
                        previous = page.evaluate("localStorage.getItem('owner')")
                        if len(snapshots) == 2:
                            assert previous == "alpha"
                            assert any(c["name"] == "owner" for c in context.cookies())
                        else:
                            assert previous is None
                            assert context.cookies() == []
                        page.evaluate("(id) => localStorage.setItem('owner', id)", account.id)
                        page.evaluate(
                            "(id) => document.cookie='owner='+id+';max-age=3600'", account.id
                        )
                        snapshots.append((manifest["fingerprint_id"], page.evaluate(PROBE)))
        assert snapshots[0] == snapshots[2], (
            "same account must retain effective and observed identity"
        )
        assert snapshots[0][0] != snapshots[1][0], "accounts need different effective fingerprints"
        assert snapshots[0][1] != snapshots[1][1], "accounts need different observed fingerprints"
    finally:
        server.shutdown()
        server.server_close()


def test_real_background_start_and_graceful_stop(catalog):
    account = catalog.accounts[0]
    other = catalog.accounts[1]
    try:
        started = start_account(catalog, account, headless=True)
        second = start_account(catalog, other, headless=True)
        assert started["state"] == "running"
        assert started["pid"] != second["pid"]
        assert started["fingerprint_id"] != second["fingerprint_id"]
        assert session_status(account)["state"] == "running"
        assert stop_account(account)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and session_status(account)["state"] != "stopped":
            time.sleep(0.1)
        assert session_status(account)["state"] == "stopped"
        assert session_status(other)["state"] == "running"
    finally:
        stop_account(account)
        stop_account(other)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and session_status(other)["state"] != "stopped":
            time.sleep(0.1)
        assert session_status(other)["state"] == "stopped"


def test_real_no_urls_skips_the_configured_site(catalog):
    """Evidence that --no-urls actually prevents navigation, not just passes a flag."""
    requested = []

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><title>should not load</title></html>")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"

    # The worker re-reads the catalog from disk, so the URL must land in the TOML.
    # Anchor on the account's id: the first `urls =` line belongs to [defaults].
    text = catalog.path.read_text(encoding="utf-8")
    marker = f'id = "{catalog.accounts[0].id}"'
    head, _, tail = text.partition(marker)
    catalog.path.write_text(
        head + marker + tail.replace('urls = ["about:blank"]', f'urls = ["{url}"]', 1)
    )
    reloaded = load_catalog(catalog.path)
    account = reloaded.accounts[0]
    assert account.urls == (url,), "test setup failed to install the local URL"

    def wait_until_stopped():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and session_status(account)["state"] != "stopped":
            time.sleep(0.1)

    try:
        start_account(reloaded, account, headless=True, no_urls=True)
        time.sleep(3)
        assert requested == [], f"navigated despite --no-urls: {requested}"
        stop_account(account)
        wait_until_stopped()

        # Control: without the flag the same account does reach the site.
        start_account(reloaded, account, headless=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not requested:
            time.sleep(0.1)
        assert requested, "control run never navigated; the test proves nothing"
    finally:
        stop_account(account)
        wait_until_stopped()
        server.shutdown()
        server.server_close()
