"""Private worker entry point; managed exclusively by accountctl."""

import argparse
import os
import signal
from pathlib import Path

import portalocker

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
    options = browser_options(account, manifest, headless=headless, proxy=proxy)
    restore_legacy_window(account)
    with sync_playwright() as playwright:
        context = playwright.firefox.launch_persistent_context(**options)
        try:
            write_json(
                account.profile / "session.json",
                {
                    "state": "running",
                    "pid": os.getpid(),
                    "token": token,
                    "fingerprint_id": manifest["fingerprint_id"],
                },
            )
            pages = context.pages
            # Keep one page so the legacy window sizing below still applies.
            urls = ("about:blank",) if no_urls else account.urls
            for index, url in enumerate(urls):
                if closing or stop_requested(account, token):
                    break
                page = pages[0] if index == 0 and pages else context.new_page()
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
                    pass
            while not once and not closing and not stop_requested(account, token):
                if not context.pages:
                    break
                try:
                    context.pages[0].wait_for_timeout(300)
                except Error:
                    # Closing the active tab is not closing the entire account.
                    if not context.pages:
                        break
        finally:
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
