"""Shard engine: an open-source anti-detect Chromium, one identity per profile.

Chromium is the only engine claude.ai's attestation lets through, and a stock
Chromium profile still shares canvas, WebGL, GPU and TLS with every other
profile on the machine. ShardBrowser (MIT) patches those inside Blink, V8 and
the network stack instead of injecting scripts, ships real device templates
(M1-M5, RTX, ...) and exposes a CDP endpoint per profile.

The SDK owns the profile format and the engine download; this module only adds
what accountctl is responsible for: where the profile lives, the location
signals taken from the verified exit, and the same start/stop discipline as the
other engines.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkspaceError

PLATFORM = "macOS" if sys.platform == "darwin" else "Windows"


def _sdk(account):
    try:
        from shardx import ShardX
    except ImportError:
        raise WorkspaceError(
            "shard 引擎需要 shardx：pip install 'account-workspaces[shard]' 后重试"
        ) from None
    # The profile (cookies, cache, fingerprint) belongs to the account directory,
    # so the whole account stays one movable unit.
    return ShardX(profiles_dir=str(account.profile / "shard"))


def runtime_identity(account=None) -> dict:
    from importlib.metadata import version

    try:
        version("shardx")
    except Exception:  # noqa: BLE001
        raise WorkspaceError(
            "shard 引擎需要 shardx：pip install 'account-workspaces[shard]' 后重试"
        ) from None
    return {"engine": "shard", "shardx": version("shardx"), "platform": PLATFORM}


def _override(profile, identity):
    """Pin the location signals; the SDK would otherwise resolve them itself."""
    if identity is None:
        return profile
    # Canvas, audio, rects and fonts are the surfaces a template does not vary, so
    # two profiles on one Mac would otherwise hash identically.
    profile = profile.set_noise("canvas", "audio", "client_rects", "fonts")
    language = identity.locale
    return profile.with_override(
        timezone=identity.timezone,
        icu_locale=language,
        navigator={
            "language": language,
            "languages": [language, language.split("-")[0]],
            "accept_language": f"{language},{language.split('-')[0]};q=0.9",
        },
    )


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
    sdk = _sdk(account)
    profile = sdk.create_profile(platform=PLATFORM)
    effective = {
        "config": {"template": profile.config.get("name"), "shard_id": profile.id},
        "prefs": {},
    }
    manifest = {
        "schema": 2,
        "engine": "shard",
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

    if manifest.get("engine") != "shard":
        raise WorkspaceError("此 profile 不是 shard 引擎；请新建账号")
    if manifest.get("account_id") != account.id:
        raise WorkspaceError("此 profile 已绑定其他账号；不能复用或复制为另一个账号")
    if manifest.get("runtime") != runtime:
        raise WorkspaceError("shardx 或引擎平台已变化；拒绝带着旧身份启动")
    if manifest.get("settings") != frozen_settings(account):
        raise WorkspaceError("窗口与固化身份不一致；请恢复配置，或创建新的账号环境")
    if not isinstance(manifest.get("effective"), dict):
        raise WorkspaceError("固化身份结构损坏")


@dataclass
class Launch:
    """What the worker needs: a CDP endpoint and a way to stop the browser."""

    cdp_url: str
    session: object

    def stop(self) -> None:
        try:
            self.session.stop()
        except Exception:  # noqa: BLE001 - stopping must not mask the real error
            pass


def launch(account, identity, *, proxy: dict | None, headless: bool, manifest: dict) -> Launch:
    sdk = _sdk(account)
    shard_id = manifest["effective"]["config"]["shard_id"]
    try:
        profile = _override(sdk.open_profile(shard_id), identity)
    except FileNotFoundError:
        raise WorkspaceError("shard profile 丢失；请从备份恢复账号目录") from None
    server = proxy["server"] if proxy else None
    if proxy and proxy.get("username"):
        scheme = server.split("://", 1)[0]
        server = f"{scheme}://{proxy['username']}:{proxy['password']}@{server.split('://', 1)[1]}"
    session = sdk.launch(profile, proxy=server, cdp=True, headless=headless)
    if not session.cdp_url:
        session.stop()
        raise WorkspaceError("shard 浏览器没有给出 CDP 端点")
    # Tooling (and a human) can attach without launching anything.
    (account.profile / "cdp.txt").write_text(session.cdp_url + "\n", encoding="utf-8")
    return Launch(cdp_url=session.cdp_url, session=session)


def noise_id(account) -> str:
    """Stable marker for logs; the SDK seeds its own per-profile noise."""
    return hashlib.sha256(str(Path(account.profile)).encode()).hexdigest()[:12]
