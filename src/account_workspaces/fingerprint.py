"""Freeze Camoufox's *effective* config, not only its BrowserForge input."""

import hashlib
import json
import sys
from dataclasses import fields, is_dataclass
from importlib.metadata import version
from pathlib import Path
from typing import get_args, get_type_hints

from .config import Account
from .errors import WorkspaceError
from .storage import atomic_write, private_directory, read_json, write_json

MANIFEST = "workspace.json"


def host_os() -> str:
    return "macos" if sys.platform == "darwin" else ("linux" if sys.platform.startswith("linux") else "windows")
DYNAMIC_CONFIG = (
    "timezone",
    "locale:language",
    "locale:region",
    "locale:script",
    "locale:all",
    "geolocation:longitude",
    "geolocation:latitude",
    "geolocation:accuracy",
    "webrtc:ipv4",
    "webrtc:ipv6",
)


def digest(data: dict) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def restore_dataclass(cls, data):
    if not is_dataclass(cls):
        return data
    hints = get_type_hints(cls)
    result = {}
    for item in fields(cls):
        value = data[item.name]
        annotation = hints[item.name]
        nested = next((t for t in get_args(annotation) if is_dataclass(t)), annotation)
        result[item.name] = (
            restore_dataclass(nested, value)
            if isinstance(value, dict) and is_dataclass(nested)
            else value
        )
    return cls(**result)


def installed_executable() -> Path:
    from camoufox.pkgman import LAUNCH_FILE, OS_NAME, camoufox_path

    # launch_path() can auto-download/replace an unsupported installation. Never use it.
    try:
        root = camoufox_path(download_if_missing=False)
        if OS_NAME == "mac":
            root = root / "Camoufox.app" / "Contents" / "Resources"
        executable = root / LAUNCH_FILE[OS_NAME]
        if not executable.is_file():
            raise FileNotFoundError
        return executable.resolve()
    except Exception:
        raise WorkspaceError(
            "Camoufox 浏览器未安装或版本不受支持；请检查 python -m camoufox fetch，"
            "已有账号升级前需备份"
        ) from None


def runtime_identity() -> dict:
    from camoufox.pkgman import installed_verstr

    executable = installed_executable()
    with executable.open("rb") as handle:
        executable_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        "camoufox": version("camoufox"),
        "browserforge": version("browserforge"),
        "playwright": version("playwright"),
        "browser": installed_verstr(),
        "executable_sha256": executable_hash,
    }


def settings_identity(account: Account) -> dict:
    """Schema 1: location was frozen into the fingerprint."""
    return {
        "window": list(account.browser.window),
        "locale": account.browser.locale,
        "timezone": account.browser.timezone,
        "geoip": account.browser.geoip,
        "legacy": account.browser.legacy,
    }


def frozen_settings(account: Account) -> dict:
    """Schema 2: location is re-derived from the exit on every start."""
    return {"window": list(account.browser.window), "legacy": account.browser.legacy}


def verify_manifest(account: Account, manifest: dict, runtime: dict) -> None:
    schema = manifest.get("schema")
    if schema not in {1, 2}:
        raise WorkspaceError("不支持的 profile 格式；不会覆盖原数据")
    if manifest.get("account_id") != account.id:
        raise WorkspaceError("此 profile 已绑定其他账号；不能复用或复制为另一个账号")
    if manifest.get("runtime") != runtime:
        raise WorkspaceError("Camoufox/BrowserForge/Playwright 或浏览器二进制已变化；拒绝指纹漂移")
    expected = settings_identity(account) if schema == 1 else frozen_settings(account)
    if manifest.get("settings") != expected:
        raise WorkspaceError("窗口、语言或时区与固化指纹不一致；请恢复配置，或创建新的账号环境")
    effective = manifest.get("effective")
    if not isinstance(effective, dict) or digest(effective) != manifest.get("fingerprint_id"):
        raise WorkspaceError("固化指纹校验失败；请从备份恢复，不会自动生成替代指纹")
    if not isinstance(effective.get("config"), dict) or not isinstance(
        effective.get("prefs"), dict
    ):
        raise WorkspaceError("固化指纹结构损坏")
    fp_path = account.profile / "fingerprint.json"
    if not fp_path.exists() or digest(read_json(fp_path)) != manifest.get("source_sha256"):
        raise WorkspaceError("原始 fingerprint.json 丢失或变化；请从备份恢复")


def _migrate(manifest: dict, account: Account) -> dict:
    """Schema 1 froze the location; schema 2 re-derives it from the exit."""
    config = dict(manifest["effective"]["config"])
    for key in DYNAMIC_CONFIG:
        config.pop(key, None)
    effective = {"config": config, "prefs": dict(manifest["effective"]["prefs"])}
    migrated = {
        **manifest,
        "schema": 2,
        "settings": frozen_settings(account),
        "effective": effective,
        "fingerprint_id": digest(effective),
    }
    return migrated


def prepare_fingerprint(account: Account, *, proxy: dict | None = None) -> dict:
    """Caller must hold the profile lock for the entire browser lifetime."""
    if account.browser.engine == "chrome":
        from . import chrome

        return chrome.prepare(account)
    from browserforge.fingerprints import Fingerprint, FingerprintGenerator, Screen
    from camoufox import DefaultAddons
    from camoufox.utils import launch_options

    runtime = runtime_identity()
    path = account.profile / MANIFEST
    if path.exists():
        manifest = read_json(path)
        verify_manifest(account, manifest, runtime)
        if manifest.get("schema") == 1 and account.browser.egress != "off":
            manifest = _migrate(manifest, account)
            write_json(path, manifest)
        return manifest
    if (account.profile / ".managed").exists():
        raise WorkspaceError("已管理 profile 的 workspace.json 丢失；请从备份恢复")
    private_directory(account.profile)
    fp_path = account.profile / "fingerprint.json"
    if not fp_path.exists() and (account.profile / "user_data").exists():
        raise WorkspaceError("已有登录数据但缺少指纹；拒绝为旧环境生成新身份")
    if fp_path.exists():
        source = read_json(fp_path)
        try:
            fingerprint = restore_dataclass(Fingerprint, source)
        except (KeyError, TypeError, ValueError):
            raise WorkspaceError("旧 fingerprint.json 格式不兼容；未修改原文件") from None
    else:
        width, height = account.browser.window
        fingerprint = FingerprintGenerator(
            browser="firefox",
            # The host's fonts and GPU are what the browser can actually render, so a
            # verified Windows identity on a Mac is a mismatch the OS cannot back up.
            os=host_os(),
            screen=Screen(
                min_width=width,
                max_width=width + 500,
                min_height=height,
                max_height=height + 500,
            ),
        ).generate()
        source = json.loads(fingerprint.dumps())
        atomic_write(fp_path, json.dumps(source, ensure_ascii=False, indent=2) + "\n")
    width, height = account.browser.window
    config = {
        "screen.width": width,
        "screen.height": height,
        "screen.availWidth": width,
        "screen.availHeight": height,
        "window.innerWidth": width,
        "window.innerHeight": height - 100,
        "window.outerWidth": width,
        "window.outerHeight": height,
    }
    # egress="off" keeps the old frozen-location behavior; anything else re-derives
    # the location at launch (see egress.py) and only the random parts stay here.
    if account.browser.egress == "off":
        if account.browser.timezone or not account.browser.legacy:
            config["timezone"] = account.browser.timezone or "UTC"
    prefs = {
        "privacy.window.maxInnerWidth": width,
        "privacy.window.maxInnerHeight": height,
        "privacy.window.maxOuterWidth": width,
        "privacy.window.maxOuterHeight": height,
        "privacy.resistFingerprinting.letterboxing": False,
        "dom.disable_window_move_resize": False,
        "browser.sessionstore.resume_from_crash": False,
        "browser.startup.page": 1,
    }
    if account.browser.legacy:
        prefs.update(
            {
                "browser.sessionstore.restore_window_behavior": 0,
                "browser.sessionstore.restore_on_demand": False,
                "toolkit.legacyUserProfileCustomizations.stylesheets": True,
            }
        )
    # launch_options fills config in place: fonts, Canvas, WebGL, locale, seeds, etc.
    # It is only used to sample the random parts; the location is applied at launch.
    # Only config/prefs are stored; the returned options with proxy/env secrets are discarded.
    launch_options(
        config=config,
        fingerprint=fingerprint,
        window=account.browser.window,
        locale=account.browser.locale,
        geoip=False,
        proxy=proxy,
        block_webrtc=account.browser.webrtc == "block",
        firefox_user_prefs=prefs,
        i_know_what_im_doing=True,
        exclude_addons=[] if account.browser.legacy else list(DefaultAddons),
        headless=True,
        env={},
    )
    effective = {"config": config, "prefs": prefs}
    manifest = {
        "schema": 2,
        "account_id": account.id,
        "runtime": runtime,
        "settings": frozen_settings(account),
        "effective": effective,
        "fingerprint_id": digest(effective),
        "source_sha256": digest(source),
    }
    write_json(path, manifest)
    atomic_write(account.profile / ".managed", "1\n")
    return manifest


def browser_options(
    account: Account,
    manifest: dict,
    *,
    headless: bool,
    proxy: dict | None,
    effective: dict | None = None,
    identity=None,
):
    """Bypass launch_options on restore: it samples random values on EVERY invocation."""
    from camoufox.utils import get_env_vars, get_target_os

    from .egress import prefs as egress_prefs

    effective = effective or manifest["effective"]
    config = dict(effective["config"])
    # BrowserForge marks every identity as DNT + GPC; together they are a much rarer
    # combination than either alone, and attestation scores notice.
    config["navigator.doNotTrack"] = "0"
    config["navigator.extraProperties.globalPrivacyControl"] = False
    # BrowserForge leaves the menu bar and the Retina density out; on a Mac the
    # viewport never equals the screen and the ratio is 2, and both are readable.
    width, height = account.browser.window
    if config.get("screen.width") == width:
        # BrowserForge pins the screen to the window; a real display is a bit larger
        # than the window, and a screen identical to the window is readable.
        config["screen.width"] = config["screen.availWidth"] = width + 30
        config["screen.height"] = height + 56
        config["screen.availHeight"] = height + 23
    if sys.platform == "darwin":
        config["screen.devicePixelRatio"] = 2
    # Synthesized input timings become human-shaped; attestation scores notice.
    config["humanize"] = True
    env = browser_environment(get_env_vars(config, get_target_os(config)))
    # Camoufox's `timezone` config key is silently ignored by the bundled build: JS still
    # reports the host zone. TZ is what the browser process actually honors, so set both.
    timezone = identity.timezone if identity is not None else config.get("timezone")
    if timezone:
        env["TZ"] = timezone
    if identity is not None:
        # Locale formatting and date names read these even when the fingerprint is right.
        env["LANG"] = env["LC_ALL"] = f"{identity.locale}.UTF-8"
    prefs = dict(effective["prefs"])
    prefs.update(egress_prefs(identity, account.browser.webrtc))
    options = {
        "executable_path": str(installed_executable()),
        "env": env,
        "firefox_user_prefs": prefs,
        "headless": headless,
        "proxy": proxy,
        "user_data_dir": str(account.profile / "user_data"),
        "viewport": {
            "width": account.browser.window[0],
            "height": account.browser.window[1]
            if account.browser.legacy
            else account.browser.window[1] - 100,
        },
        "args": [],
    }
    if sys.platform == "darwin":
        # Retina; without this a headless or freshly opened window reports 1.
        options["device_scale_factor"] = 2
    return options


def restore_legacy_window(account: Account) -> None:
    """Preserve the old -w behavior without deleting corrupt xulstore files."""
    if not account.browser.legacy:
        return
    path = account.profile / "user_data" / "xulstore.json"
    if not path.exists():
        return
    try:
        data = read_json(path)
        window = data.get("chrome://browser/content/browser.xhtml", {}).get("main-window")
        if isinstance(window, dict):
            window["width"], window["height"] = map(str, account.browser.window)
            write_json(path, data)
    except WorkspaceError:
        # Leave the original untouched; pinned launch preferences remain authoritative.
        pass


def browser_environment(fingerprint_env: dict) -> dict:
    import os

    # Browser processes don't need the CLI's credentials. Use a small OS/session allowlist.
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "SYSTEMROOT",
        "WINDIR",
        "LOCALAPPDATA",
        "APPDATA",
        "NO_PROXY",
        "no_proxy",
    }
    return {**{k: v for k, v in os.environ.items() if k in allowed}, **fingerprint_env}
