"""The account catalog owns validation, inheritance, paths and atomic edits."""

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import portalocker
import tomlkit

from .errors import WorkspaceError
from .storage import atomic_write, lock

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
BROWSER_FIELDS = {
    "window",
    "proxy",
    "proxy_ref",
    "geoip",
    "locale",
    "timezone",
    "legacy",
    "proxy_from_meta",
    "proxy_from_account",
}
ACCOUNT_FIELDS = {
    "id",
    "label",
    "provider",
    "tags",
    "urls",
    "profile",
    "enabled",
    "browser",
    "credentials",
}
DEFAULT_DOCUMENT = """# 本机账号目录；不要提交到 Git。参见 accounts.example.toml。
version = 1
state_dir = ".account-workspaces"

[defaults]
urls = ["about:blank"]

[defaults.browser]
window = [1440, 900]
"""


def _unknown(data: dict, allowed: set[str], location: str) -> None:
    if not isinstance(data, dict):
        raise WorkspaceError(f"{location} 必须是 TOML table")
    if set(data) - allowed:
        raise WorkspaceError(f"{location} 含不支持的字段；请检查配置示例（禁止明文密码）")


def _text(value, location: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise WorkspaceError(f"{location} 必须是非空字符串")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorkspaceError(f"{location} 不能含控制字符")
    return value


def _strings(value, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WorkspaceError(f"{location} 必须是字符串数组")
    return tuple(_text(item, location) for item in value)


def validate_url(value: str) -> str:
    _text(value, "URL")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if value != "about:blank" and not valid:
        raise WorkspaceError("启动 URL 只支持 http(s) 或 about:blank，且不能包含凭据")
    return value


def validate_reference(value: str) -> str:
    _text(value, "凭据引用")
    if value.startswith("env://") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value[6:]):
        return value
    if value.startswith("op://"):
        parts = value[5:].split("/")
        if len(parts) in {3, 4} and all(parts):
            return value
    raise WorkspaceError("凭据字段只接受 op://vault/item/field 或 env://VARIABLE 引用")


@dataclass(frozen=True)
class BrowserSettings:
    window: tuple[int, int] = (1440, 900)
    proxy: str | None = field(default=None, repr=False)
    proxy_ref: str | None = field(default=None, repr=False)
    geoip: bool = False
    locale: str | None = None
    timezone: str | None = None
    legacy: bool = False
    proxy_from_meta: bool = False
    proxy_from_account: str | None = None
    resolved_proxy_meta: Path | None = field(default=None, repr=False)
    resolved_proxy_op_account: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    provider: str
    tags: tuple[str, ...]
    urls: tuple[str, ...]
    profile: Path
    enabled: bool
    browser: BrowserSettings
    credentials: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Catalog:
    path: Path
    state_dir: Path
    accounts: tuple[Account, ...]

    def select(self, ids: list[str], tags: list[str], all_accounts: bool) -> tuple[Account, ...]:
        if not ids and not tags and not all_accounts:
            raise WorkspaceError("请指定账号 ID、--tag 或 --all")
        if ids and (tags or all_accounts):
            raise WorkspaceError("账号 ID 不能与 --tag / --all 混用")
        if tags and all_accounts:
            raise WorkspaceError("--tag 不能与 --all 混用")
        known = {account.id: account for account in self.accounts}
        if any(account_id not in known for account_id in ids):
            raise WorkspaceError("存在未知账号 ID；请先执行 accountctl list")
        selected = (
            tuple(known[account_id] for account_id in dict.fromkeys(ids))
            if ids
            else tuple(a for a in self.accounts if all_accounts or set(tags) <= set(a.tags))
        )
        if ids and any(not a.enabled for a in selected):
            raise WorkspaceError("选中的账号已禁用")
        selected = tuple(a for a in selected if a.enabled)
        if not selected:
            raise WorkspaceError("没有匹配的启用账号")
        return selected


def _browser(data: dict) -> BrowserSettings:
    _unknown(data, BROWSER_FIELDS, "browser")
    window = data.get("window", [1440, 900])
    if (
        not isinstance(window, list)
        or len(window) != 2
        or any(type(n) is not int or n < 320 or n > 7680 for n in window)
    ):
        raise WorkspaceError("browser.window 必须是两个 320..7680 的整数")
    geoip = data.get("geoip", False)
    if type(geoip) is not bool:
        raise WorkspaceError("browser.geoip 必须是布尔值")
    legacy = data.get("legacy", False)
    proxy_from_meta = data.get("proxy_from_meta", False)
    if type(legacy) is not bool or type(proxy_from_meta) is not bool:
        raise WorkspaceError("legacy / proxy_from_meta 必须是布尔值")
    if proxy_from_meta and not legacy:
        raise WorkspaceError("proxy_from_meta 只用于接入旧 profile")
    proxy, proxy_ref = data.get("proxy"), data.get("proxy_ref")
    proxy_from_account = data.get("proxy_from_account")
    if proxy_from_account is not None:
        _text(proxy_from_account, "proxy_from_account")
    if sum(bool(v) for v in (proxy, proxy_ref, proxy_from_meta, proxy_from_account)) > 1:
        raise WorkspaceError("proxy、proxy_ref、proxy_from_meta、proxy_from_account 只能选择一种")
    if proxy is not None:
        from .network import parse_proxy

        parsed = parse_proxy(_text(proxy, "browser.proxy"))
        if "username" in parsed or "password" in parsed:
            raise WorkspaceError("带认证信息的代理必须使用 proxy_ref，不能写入 TOML")
    if proxy_ref is not None:
        validate_reference(proxy_ref)
    locale = _text(data["locale"], "locale") if "locale" in data else None
    from language_tags import tags

    if locale and not tags.check(locale):
        raise WorkspaceError("locale 必须是有效的语言标签，例如 en-US / zh-CN")
    timezone = _text(data["timezone"], "timezone") if "timezone" in data else None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        if timezone:
            ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise WorkspaceError("timezone 必须是有效的 IANA 时区，例如 Asia/Shanghai") from None
    if geoip and (locale or timezone):
        raise WorkspaceError(
            "geoip=true 时语言与时区从出口 IP 初始化；不能同时指定 locale/timezone"
        )
    return BrowserSettings(
        tuple(window),
        proxy,
        proxy_ref,
        geoip,
        locale,
        timezone,
        legacy,
        proxy_from_meta,
        proxy_from_account,
    )


def parse_catalog(path: Path, raw: dict) -> Catalog:
    path = path.expanduser().resolve()
    _unknown(raw, {"version", "state_dir", "defaults", "accounts"}, "根配置")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise WorkspaceError("不支持的配置版本；version 必须是 1")
    state = (
        path.parent
        / Path(_text(raw.get("state_dir", ".account-workspaces"), "state_dir")).expanduser()
    ).resolve()
    defaults = raw.get("defaults", {})
    _unknown(defaults, {"browser", "urls"}, "defaults")
    browser_defaults = defaults.get("browser", {})
    _browser(browser_defaults)
    default_urls = _strings(defaults.get("urls", ["about:blank"]), "defaults.urls")
    for url in default_urls:
        validate_url(url)
    rows = raw.get("accounts", [])
    if not isinstance(rows, list):
        raise WorkspaceError("accounts 必须是 table 数组：[[accounts]]")
    accounts, ids, profiles = [], set(), set()
    for row in rows:
        _unknown(row, ACCOUNT_FIELDS, "accounts")
        account_id = _text(row.get("id"), "account.id")
        if not ID_PATTERN.fullmatch(account_id):
            raise WorkspaceError("账号 ID 必须为 1..64 位小写字母/数字/下划线/短横线")
        profile = (
            (path.parent / Path(_text(row["profile"], "profile")).expanduser()).resolve()
            if "profile" in row
            else (state / "profiles" / account_id).resolve()
        )
        if account_id in ids:
            raise WorkspaceError(f"重复账号 ID：{account_id}")
        if any(
            profile == other or profile in other.parents or other in profile.parents
            for other in profiles
        ):
            raise WorkspaceError("多个账号不能共享或嵌套 profile 目录")
        if profile in {path.parent, state, Path.home(), Path(profile.anchor)}:
            raise WorkspaceError("profile 必须是专用子目录，不能指向仓库、状态目录或主目录")
        if profile in path.parents or profile in state.parents:
            raise WorkspaceError("profile 不能是配置文件或状态目录的上级目录")
        ids.add(account_id)
        profiles.add(profile)
        overrides = row.get("browser", {})
        _unknown(overrides, BROWSER_FIELDS, "account.browser")
        merged = {**browser_defaults, **overrides}
        if overrides.get("legacy") or overrides.get("geoip"):
            for key in ("locale", "timezone"):
                if key not in overrides:
                    merged.pop(key, None)
        if overrides.get("proxy_from_meta"):
            merged.pop("proxy", None)
            merged.pop("proxy_ref", None)
        if overrides.get("proxy_from_account"):
            for key in ("proxy", "proxy_ref", "proxy_from_meta"):
                merged.pop(key, None)
        # Overriding one proxy source also replaces the other inherited source.
        if "proxy_ref" in overrides and "proxy" not in overrides:
            merged.pop("proxy", None)
        if "proxy" in overrides and "proxy_ref" not in overrides:
            merged.pop("proxy_ref", None)
        browser = _browser(merged)
        urls = _strings(row["urls"], "account.urls") if "urls" in row else default_urls
        if not urls:
            raise WorkspaceError("每个账号至少需要一个启动 URL")
        for url in urls:
            validate_url(url)
        enabled = row.get("enabled", True)
        if type(enabled) is not bool:
            raise WorkspaceError("enabled 必须是布尔值")
        credentials = row.get("credentials", {})
        _unknown(credentials, {"username_ref", "password_ref", "op_account"}, "credentials")
        for key, value in credentials.items():
            _text(value, key) if key == "op_account" else validate_reference(value)
        accounts.append(
            Account(
                account_id,
                _text(row.get("label", account_id), "label"),
                _text(row.get("provider", "custom"), "provider"),
                _strings(row.get("tags", []), "tags"),
                tuple(urls),
                profile,
                enabled,
                browser,
                dict(credentials),
            )
        )
    by_id = {a.id: a for a in accounts}

    def resolve_proxy(account: Account, visited: set[str]) -> Account:
        reference = account.browser.proxy_from_account
        if not reference:
            return account
        if reference in visited or reference not in by_id:
            raise WorkspaceError("proxy_from_account 存在循环或引用了未知账号")
        source = resolve_proxy(by_id[reference], visited | {reference})
        settings = source.browser
        meta = settings.resolved_proxy_meta
        if settings.proxy_from_meta:
            meta = source.profile / "meta.json"
        if not (meta or settings.proxy or settings.proxy_ref):
            raise WorkspaceError("proxy_from_account 引用的账号没有配置代理")
        return replace(
            account,
            browser=replace(
                account.browser,
                proxy=settings.proxy,
                proxy_ref=settings.proxy_ref,
                resolved_proxy_meta=meta,
                resolved_proxy_op_account=(
                    settings.resolved_proxy_op_account or source.credentials.get("op_account")
                ),
            ),
        )

    return Catalog(path, state, tuple(resolve_proxy(a, {a.id}) for a in accounts))


def load_catalog(path: Path) -> Catalog:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WorkspaceError("找不到配置；先执行 accountctl init 或使用 --config") from None
    except (OSError, ValueError):
        raise WorkspaceError("无法解析 TOML 配置；请检查语法（错误原文已隐藏）") from None
    return parse_catalog(path, raw)


def initialize(path: Path) -> Catalog:
    try:
        with lock(path.with_suffix(path.suffix + ".lock")):
            if path.exists():
                raise WorkspaceError("配置已存在，不会覆盖")
            atomic_write(path, DEFAULT_DOCUMENT)
    except portalocker.exceptions.LockException:
        raise WorkspaceError("配置正在被另一个进程修改，请稍后重试") from None
    return load_catalog(path)


def add_accounts(path: Path, rows: list[dict]) -> Catalog:
    try:
        with lock(path.with_suffix(path.suffix + ".lock")):
            load_catalog(path)
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
            if "accounts" not in document:
                document["accounts"] = tomlkit.aot()
            for row in rows:
                document["accounts"].append(row)
            text = tomlkit.dumps(document)
            catalog = parse_catalog(path, tomllib.loads(text))
            atomic_write(path, text)
            return catalog
    except portalocker.exceptions.LockException:
        raise WorkspaceError("配置正在被另一个进程修改，请稍后重试") from None


def default_config() -> Path:
    return Path(os.environ.get("ACCOUNT_WORKSPACES_CONFIG", "accounts.toml")).expanduser().resolve()
