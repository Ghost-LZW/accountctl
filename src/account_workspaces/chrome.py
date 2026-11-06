"""Chrome engine: one account, one real Chrome identity.

Camoufox makes several identities look different from each other; this engine
makes a single one look utterly ordinary. Nothing is randomised: the browser is
the installed Google Chrome, so canvas, WebGL, fonts and the UA are whatever a
normal Mac reports. That is what attestation-heavy sign-in flows score highest.

The trade-off lives in the catalog, not here: profiles of this engine are nearly
indistinguishable from one another, so use it when one account must pass for a
normal person, and Camoufox when many accounts must not correlate.

Only the location signals stay dynamic, exactly as in the Camoufox engine:
timezone and locale are overridden per launch, and WebRTC is forced through the
proxy so it cannot announce the host's address.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .errors import WorkspaceError

MAC_EXECUTABLE = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
FALLBACKS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def installed_executable(account=None) -> Path:
    binary = getattr(getattr(account, "browser", None), "binary", None)
    if binary:
        path = Path(binary).expanduser()
        if path.is_file():
            return path.resolve()
        raise WorkspaceError(f"browser.binary 指向的文件不存在：{binary}")
    override = os.getenv("ACCOUNTCTL_CHROME")
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path.resolve()
        raise WorkspaceError(f"ACCOUNTCTL_CHROME 指向的文件不存在：{override}")
    if MAC_EXECUTABLE.is_file():
        return MAC_EXECUTABLE.resolve()
    for name in FALLBACKS:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    raise WorkspaceError("找不到 Google Chrome；请安装，或用 browser.binary 指定路径")


def version_of(executable: Path) -> str:
    try:
        output = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        output = ""
    # "Google Chrome 141.0.7390.55" and "Chromium 141.0.0.0" both end in the version.
    version = output.rsplit(" ", 1)[-1] if output else ""
    if not version or not version[0].isdigit():
        raise WorkspaceError("无法读取 Chrome 版本；请检查安装是否完整")
    return version


def runtime_identity(account=None) -> dict:
    """Chrome updates on its own, so pin the major version instead of the binary."""
    from .fingerprint import version

    executable = installed_executable(account)
    browser = version_of(executable)
    return {
        "engine": "chrome",
        "playwright": version("playwright"),
        "browser": browser,
        "major": browser.split(".", 1)[0],
    }


def prepare(account) -> dict:
    """Caller must hold the profile lock for the entire browser lifetime."""
    from .fingerprint import MANIFEST, digest, frozen_settings
    from .storage import atomic_write, private_directory, read_json, write_json

    runtime = runtime_identity(account)
    path = account.profile / MANIFEST
    if path.exists():
        manifest = read_json(path)
        verify(account, manifest, runtime)
        return manifest
    if (account.profile / ".managed").exists():
        raise WorkspaceError("已管理 profile 的 workspace.json 丢失；请从备份恢复")
    private_directory(account.profile)
    # Nothing random: the real browser is the identity. Only the window the user
    # chose is frozen, because changing it later would shift every screen signal.
    effective = {"config": {"window": list(account.browser.window)}, "prefs": {}}
    manifest = {
        "schema": 2,
        "engine": "chrome",
        "account_id": account.id,
        "runtime": runtime,
        "settings": frozen_settings(account),
        "effective": effective,
        "fingerprint_id": digest(effective),
    }
    write_json(path, manifest)
    atomic_write(account.profile / ".managed", "1\n")
    return manifest


def verify(account, manifest: dict, runtime: dict) -> None:
    from .fingerprint import frozen_settings

    if manifest.get("engine") != "chrome":
        raise WorkspaceError("此 profile 属于 camoufox；chrome 需要一个新账号，不能复用旧目录")
    if manifest.get("account_id") != account.id:
        raise WorkspaceError("此 profile 已绑定其他账号；不能复用或复制为另一个账号")
    if manifest.get("runtime") != runtime:
        raise WorkspaceError("Chrome 大版本或 Playwright 已变化；拒绝带着旧身份启动")
    if manifest.get("settings") != frozen_settings(account):
        raise WorkspaceError("窗口与固化身份不一致；请恢复配置，或创建新的账号环境")
    effective = manifest.get("effective")
    if not isinstance(effective, dict) or not isinstance(effective.get("config"), dict):
        raise WorkspaceError("固化身份结构损坏")


def _args(account, identity, *, proxy: dict | None, headless: bool) -> list[str]:
    width, height = account.browser.window
    args = [
        f"--user-data-dir={account.profile / 'user_data'}",
        f"--window-size={width},{height}",
        "--window-position=40,40",
        "--no-first-run",
        "--no-default-browser-check",
        # CDP still sets navigator.webdriver without this; Cloudflare notices. Chrome
        # prints an "unsupported flag" banner for it, which is only cosmetic.
        "--disable-blink-features=AutomationControlled",
        # Chrome then writes the chosen port to user_data/DevToolsActivePort, so the
        # port is discovered instead of reserved.
        "--remote-debugging-port=0",
    ]
    if identity is not None:
        args.append(f"--lang={identity.locale}")
    if headless:
        args.append("--headless=new")
    if account.browser.webrtc != "keep":
        # Chrome cannot rewrite WebRTC candidates, but it can stop using UDP that
        # would bypass the proxy -- so what upstreams see is the exit, not the Mac.
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    if proxy:
        args.append(f"--proxy-server={proxy['server']}")
    return args


def spawn(account, identity, *, proxy: dict | None, headless: bool = False):
    """Start Chrome on its own, so Cloudflare sees a plain browser before any CDP.

    Playwright's own launch attaches a debugger before the first navigation and
    leaves a pile of `--disable-*` switches behind; on claude.ai that made the
    managed challenge retry forever. Launching the binary ourselves and attaching
    afterwards passes. Returns the process.
    """
    env = dict(os.environ)
    if identity is not None:
        # Both are read by the browser process itself, so every tab and worker
        # inherits the exit's location without per-page emulation.
        env["TZ"] = identity.timezone
        env["LANG"] = env["LC_ALL"] = f"{identity.locale}.UTF-8"
    return subprocess.Popen(
        [str(installed_executable(account)), *_args(
            account, identity, proxy=proxy, headless=headless
        )],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


def connect(playwright, account, process, *, timeout: float = 30.0):
    """Wait for DevToolsActivePort, then attach; the context is the real profile."""
    import time

    from .errors import WorkspaceError

    path = account.profile / "user_data" / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                port = path.read_text(encoding="utf-8").split()[0]
            except (OSError, IndexError):
                pass
            else:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                browser._accountctl_process = process  # noqa: SLF001 - for shutdown
                return browser
        if process.poll() is not None:
            raise WorkspaceError("Chrome 进程启动失败；请检查 doctor")
        time.sleep(0.1)
    raise WorkspaceError("等待 Chrome 调试端口超时；请检查是否有旧窗口占用 profile")


def prepare_context(context, identity) -> None:
    """Apply what a launch option would have applied, now that we attach instead."""
    if identity is not None:
        try:
            context.grant_permissions(["geolocation"])
        except Exception:  # noqa: BLE001 - permission API differs on older builds
            pass


def apply_page(page, identity) -> None:
    """Per-page emulation keeps a new tab from falling back to the host location."""
    if identity is None:
        return
    try:
        session = page.context.new_cdp_session(page)
        session.send(
            "Emulation.setTimezoneOverride", {"timezoneId": identity.timezone}
        )
        session.send("Emulation.setLocaleOverride", {"locale": identity.locale})
        if identity.ip != "0.0.0.0":
            session.send(
                "Emulation.setGeolocationOverride",
                {
                    "latitude": identity.latitude,
                    "longitude": identity.longitude,
                    "accuracy": identity.accuracy,
                },
            )
    except Exception:  # noqa: BLE001 - emulation is a refinement, not a gate
        pass
