"""Measure what a fresh account of an engine actually looks like.

The cheap question this answers is "would a new account be distinguishable?" --
without spending an email, a code and two minutes of Cloudflare on every guess.
It builds a throwaway profile, launches that engine once, reads the signals back
and checks them against the exit identity.

Camoufox and Chrome are probed the same way, so `accountctl probe a` /
`accountctl probe b` diff cleanly. Nothing here touches a logged-in profile.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from .errors import WorkspaceError

# Runs on about:blank. Everything is synchronous and self-contained: no network,
# no permissions, no geolocation prompt.
HARNESS = """() => {
  const out = {};
  out.ua = navigator.userAgent;
  out.platform = navigator.platform;
  out.languages = navigator.languages;
  out.language = navigator.language;
  out.hardwareConcurrency = navigator.hardwareConcurrency;
  out.deviceMemory = navigator.deviceMemory ?? null;
  out.webdriver = navigator.webdriver;
  out.doNotTrack = navigator.doNotTrack;
  out.globalPrivacyControl = navigator.globalPrivacyControl ?? null;
  out.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.screen = [screen.width, screen.height, screen.availWidth, screen.availHeight];
  out.devicePixelRatio = devicePixelRatio;
  out.colorDepth = screen.colorDepth;
  try {
    const c = document.createElement('canvas');
    c.width = 220; c.height = 60;
    const x = c.getContext('2d');
    const g = x.createLinearGradient(0, 0, 220, 60);
    g.addColorStop(0, '#123'); g.addColorStop(1, '#fd0');
    x.fillStyle = g; x.fillRect(0, 0, 220, 60);
    x.font = '20px serif'; x.fillText('fingerprint', 4, 40);
    const url1 = c.toDataURL(); const url2 = c.toDataURL();
    let h = 5381;
    for (let i = 0; i < url1.length; i++) h = ((h * 33) ^ url1.charCodeAt(i)) >>> 0;
    out.canvasHash = h;
    out.canvasStable = url1 === url2;
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      out.webglVendor = gl.getParameter(gl.VENDOR);
      out.webglRenderer = gl.getParameter(gl.RENDERER);
      out.webglUnmaskedVendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null;
      out.webglUnmaskedRenderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null;
    }
    const probe = document.createElement('canvas').getContext('2d');
    probe.font = '16px monospace';
    out.fontWidth = probe.measureText('mmmmmmmmmmlli').width;
  } catch (e) { out.error = String(e); }
  return out;
}"""


def _account_for(account, profile: Path):
    from dataclasses import replace

    return replace(account, profile=profile)


def _chrome(account, identity, proxy, login=False) -> dict:
    from playwright.sync_api import sync_playwright

    from . import chrome as engine

    process = engine.spawn(account, identity, proxy=proxy)
    try:
        with sync_playwright() as playwright:
            browser = engine.connect(playwright, account, process)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            engine.apply_page(page, account, identity)
            page.goto("about:blank", wait_until="domcontentloaded")
            data = page.evaluate(HARNESS)
            if login:
                data["login"] = login_check(page)
            browser.close()
            return data
    finally:
        if process.poll() is None:
            process.terminate()


def _camoufox(account, identity, proxy, login=False) -> dict:
    from playwright.sync_api import sync_playwright

    from . import egress
    from .fingerprint import browser_options, prepare_fingerprint

    manifest = prepare_fingerprint(account, proxy=proxy)
    effective = {
        **manifest["effective"],
        "config": egress.apply(manifest["effective"]["config"], identity, account.browser.webrtc),
    }
    # Headed: a headless Firefox reports devicePixelRatio 1, which no Retina Mac has.
    options = browser_options(
        account, manifest, headless=False, proxy=proxy, effective=effective, identity=identity
    )
    with sync_playwright() as playwright:
        context = playwright.firefox.launch_persistent_context(**options)
        try:
            page = context.new_page()
            page.goto("about:blank", wait_until="domcontentloaded")
            data = page.evaluate(HARNESS)
            if login:
                data["login"] = login_check(page)
            return data
        finally:
            context.close()


def _normalise(ua: str) -> str:
    """Firefox builds report a version; flag the ones that are a beta build."""
    return ua


DUMMY = "probe@example.com"


def login_check(page, *, timeout: float = 25.0) -> dict:
    """Push the sign-in page up to the code screen without needing an inbox.

    A throwaway address still triggers hCaptcha and Cloudflare, which are exactly
    the gates that differ between engines, so this scores them for free.
    """
    from time import monotonic

    page.goto("https://claude.ai/login", wait_until="domcontentloaded", timeout=45_000)
    # The interstitial is served first and normally resolves itself; only its
    # persistence is a failure, so wait for the form instead of sampling instantly.
    cloudflare = True
    for _ in range(30):
        if page.query_selector("#email"):
            cloudflare = False
            break
        page.wait_for_timeout(500)
    if cloudflare:
        return {"cloudflare": True, "challenge": False, "elapsed": None, "status": None, "pass": False}
    started = monotonic()
    page.fill("#email", DUMMY)
    try:
        with page.expect_response(
            lambda r: "/api/auth/send_magic_link" in r.url, timeout=timeout * 1000
        ) as answer:
            page.click("button[type=submit]")
        status = answer.value.status
    except Exception:  # noqa: BLE001 - a stall is the result we want to record
        status = None
    elapsed = round(monotonic() - started, 1)
    challenge = page.evaluate(
        """() => [...document.querySelectorAll('iframe')].some(
            f => /hcaptcha/.test(f.src) && f.offsetWidth > 100)"""
    )
    return {
        "cloudflare": cloudflare,
        "challenge": challenge,
        "elapsed": elapsed,
        "status": status,
        "pass": status == 200 and not cloudflare and not challenge,
    }


def findings(observed: dict, identity) -> list[str]:
    problems = []
    if identity is None:
        return problems
    if observed.get("timezone") != identity.timezone:
        problems.append(f"时区 {observed.get('timezone')} != {identity.timezone}")
    if (observed.get("language") or "").lower() != identity.locale.lower():
        problems.append(f"语言 {observed.get('language')} != {identity.locale}")
    if observed.get("webdriver"):
        problems.append("navigator.webdriver 为 true")
    renderer = observed.get("webglUnmaskedRenderer") or observed.get("webglRenderer") or ""
    if "or similar" in renderer.lower():
        problems.append(f"WebGL 是通用串：{renderer}")
    if not renderer:
        problems.append("没有 WebGL 渲染器")
    if observed.get("doNotTrack") not in (None, "0", "unspecified"):
        problems.append(f"doNotTrack={observed.get('doNotTrack')}")
    if observed.get("globalPrivacyControl"):
        problems.append("globalPrivacyControl 为 true")
    if not observed.get("canvasStable"):
        problems.append("同一 canvas 两次读取不一致")
    ua = observed.get("ua") or ""
    if "Firefox/" in ua:
        reported = ua.split("Firefox/")[-1]
        if not reported.split(".")[0].isdigit():
            problems.append(f"UA 版本无法解析：{reported}")
    return problems


def run(account, proxy, identity, *, login: bool = False) -> dict:
    """Launch a throwaway profile of this account's engine and measure it."""
    from .storage import private_directory

    root = account.profile.parent / f".probe-{account.id}-{int(time.time())}"
    profile = root / "profile"
    private_directory(profile)
    probe_account = _account_for(account, profile)
    started = time.monotonic()
    try:
        if account.browser.engine == "chrome":
            observed = _chrome(probe_account, identity, proxy, login)
        else:
            observed = _camoufox(probe_account, identity, proxy, login)
        elapsed = round(time.monotonic() - started, 1)
        return {
            "engine": account.browser.engine,
            "elapsed": elapsed,
            "observed": observed,
            "findings": findings(observed, identity)
            + (login_findings({"observed": observed}) if login else []),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def login_findings(result: dict) -> list[str]:
    login = result["observed"].get("login")
    if not login:
        return []
    problems = []
    if login["cloudflare"]:
        problems.append("Cloudflare 触发人工验证")
    if login["challenge"]:
        problems.append("hCaptcha 要求人解（静默失败）")
    if login["status"] != 200:
        problems.append(f"send_magic_link 未成功：{login['status']}")
    return problems


def render(result: dict) -> str:
    identity_note = "；".join(result["findings"]) or "无异常"
    lines = [
        f"引擎 {result['engine']}，{result['elapsed']}s",
        "命中项：" + identity_note,
        json.dumps(result["observed"], ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)
