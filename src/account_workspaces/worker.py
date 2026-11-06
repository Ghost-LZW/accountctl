"""Private worker entry point; managed exclusively by accountctl."""

import argparse
import os
import signal
from pathlib import Path
from urllib.parse import urlsplit

import portalocker

from . import egress
from .browser_patch import ensure_patched
from .config import Account, load_catalog
from .errors import WorkspaceError
from .fingerprint import browser_options, prepare_fingerprint, restore_legacy_window
from .lifecycle import profile_lock, stop_requested
from .network import account_proxy
from .storage import lock, write_json


def run_session(
    account: Account, token: str, *, headless: bool, once: bool = False, no_urls: bool = False
) -> None:
    from playwright.sync_api import Error, sync_playwright

    closing = False

    def request_close(*_):
        nonlocal closing
        closing = True

    signal.signal(signal.SIGTERM, request_close)
    signal.signal(signal.SIGINT, request_close)
    proxy = account_proxy(account)
    manifest = prepare_fingerprint(account, proxy=proxy)
    if closing or stop_requested(account, token):
        return
    # The exit is resolved on every start, before anything is launched. If it cannot be
    # confirmed the session does not start at all, so the browser never appears somewhere
    # it cannot prove it came from.
    if account.browser.egress != "off":
        identity = egress.resolve(account, proxy)
        egress.remember(account, identity)
    else:
        # No probe: still honor an explicitly pinned locale/timezone, but leave the
        # frozen fingerprint's geolocation alone.
        identity = egress.identity_from_config(account)
    effective = manifest["effective"]
    chrome = account.browser.engine == "chrome"
    if identity is not None and not chrome:
        effective = {
            **effective,
            "config": egress.apply(effective["config"], identity, account.browser.webrtc),
        }
    session = {
        "state": "running",
        "pid": os.getpid(),
        "token": token,
        "fingerprint_id": manifest["fingerprint_id"],
    }
    if identity is not None:
        session["egress"] = {
            "ip": identity.ip,
            "country": identity.country,
            "timezone": identity.timezone,
            "locale": identity.locale,
        }
    if chrome:
        from . import chrome as engine

        process = engine.spawn(account, identity, proxy=proxy, headless=headless)
        session["pid"] = process.pid
    else:
        process = None
        options = browser_options(
            account, manifest, headless=headless, proxy=proxy, effective=effective,
            identity=identity,
        )
        restore_legacy_window(account)
    with sync_playwright() as playwright:
        if chrome:
            browser = engine.connect(playwright, account, process)
            context = browser.contexts[0]
            engine.prepare_context(context, identity)
            if proxy and proxy.get("username"):
                context.set_http_credentials(proxy)
        else:
            browser = None
            context = playwright.firefox.launch_persistent_context(**options)
        try:
            write_json(account.profile / "session.json", session)
            pages = context.pages
            verified = False
            if identity is not None and not chrome:
                try:
                    context.grant_permissions(["geolocation"])
                except Error:
                    pass
            # Keep one page so the legacy window sizing below still applies.
            urls = ("about:blank",) if no_urls else account.urls
            for index, url in enumerate(urls):
                if closing or stop_requested(account, token):
                    break
                page = pages[0] if index == 0 and pages else context.new_page()
                if chrome:
                    engine.apply_page(page, account, identity)
                if index == 0 and account.browser.legacy:
                    width, height = account.browser.window
                    try:
                        page.set_viewport_size({"width": width, "height": height})
                        page.evaluate(f"window.resizeTo({width}, {height})")
                    except Error:
                        pass
                # Login/network failures shouldn't destroy a usable browser session.
                try:
                    page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                except Error:
                    continue
                if identity is not None and not verified and url != "about:blank":
                    verified = True
                    try:
                        parts = urlsplit(url)
                        context.grant_permissions(
                            ["geolocation"], origin=f"{parts.scheme}://{parts.netloc}"
                        )
                    except (Error, ValueError):
                        pass
                    session.update(egress.verify_page(page, identity))
                    write_json(account.profile / "session.json", session)
                    if not session["egress_verified"] and account.browser.egress in {
                        "auto",
                        "strict",
                    }:
                        raise WorkspaceError("浏览器实际特征与出口不符：" + session["egress_error"])
            while not once and not closing and not stop_requested(account, token):
                if not context.pages:
                    # Chrome quits when its last window closes; keep the session up by
                    # re-opening a tab instead of treating that as "account stopped".
                    if not chrome:
                        break
                    try:
                        context.new_page()
                    except Error:
                        break
                try:
                    context.pages[0].wait_for_timeout(300)
                except Error:
                    # Closing the active tab is not closing the entire account.
                    if not context.pages:
                        break
        finally:
            if browser is not None:
                # A CDP-attached context only detaches on close; the browser itself
                # must be asked to quit, or it would outlive the worker.
                try:
                    browser.close()
                except Error:
                    pass
                try:
                    process.wait(timeout=10)
                except Exception:  # noqa: BLE001 - a stuck Chrome still gets stopped
                    process.terminate()
            else:
                context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--account", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-urls", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    # Spawned as its own process, so it repairs the pin independently of the CLI.
    ensure_patched()
    catalog = load_catalog(args.config)
    account = catalog.select([args.account], [], False)[0]
    try:
        with lock(profile_lock(account)):
            write_json(
                account.profile / "session.json",
                {
                    "state": "starting",
                    "pid": os.getpid(),
                    "token": args.token,
                },
            )
            try:
                run_session(
                    account,
                    args.token,
                    headless=args.headless,
                    once=args.once,
                    no_urls=args.no_urls,
                )
            except Exception as error:
                message = (
                    str(error)
                    if isinstance(error, WorkspaceError)
                    else ("浏览器启动/运行失败；请检查 doctor、代理及 profile 是否被旧脚本占用")
                )
                write_json(
                    account.profile / "session.json",
                    {
                        "state": "failed",
                        "pid": None,
                        "token": args.token,
                        "error": message,
                    },
                )
                return 1
            write_json(
                account.profile / "session.json",
                {
                    "state": "stopped",
                    "pid": None,
                    "token": args.token,
                },
            )
    except portalocker.exceptions.LockException:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
