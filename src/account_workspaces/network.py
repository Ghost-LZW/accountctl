"""Proxy compatibility, including the original Camoufox NO_PROXY regression fix."""

import os
from urllib.parse import unquote, urlsplit

from .errors import WorkspaceError

LOCAL_NO_PROXY = (
    "localhost",
    "127.0.0.1",
    "127.0.0.0/8",
    "::1",
    "0.0.0.0",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    ".local",
)


def merge_no_proxy(existing: str, additions: tuple[str, ...]) -> str:
    entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
    seen = {entry.lower() for entry in entries}
    for entry in additions:
        if entry.lower() not in seen:
            entries.append(entry)
            seen.add(entry.lower())
    return ",".join(entries)


def ensure_local_no_proxy() -> str:
    existing = ",".join(filter(None, (os.getenv("NO_PROXY"), os.getenv("no_proxy"))))
    merged = merge_no_proxy(existing, LOCAL_NO_PROXY)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged
    return merged


def parse_proxy(value: str) -> dict[str, str]:
    try:
        if not isinstance(value, str) or any(ord(c) < 33 for c in value):
            raise ValueError
        parsed = urlsplit(value if "://" in value else f"http://{value}")
        if (
            parsed.scheme not in {"http", "https", "socks5"}
            or not parsed.hostname
            or not parsed.port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
        if parsed.username is not None:
            result["username"] = unquote(parsed.username)
        if parsed.password is not None:
            result["password"] = unquote(parsed.password)
        return result
    except (ValueError, TypeError):
        raise WorkspaceError(
            "代理格式错误；需要 http(s)://host:port 或 socks5://host:port"
        ) from None


def build_proxy_config(value: str) -> tuple[dict[str, str], str]:
    return parse_proxy(value), ensure_local_no_proxy()


def account_proxy(account) -> dict[str, str] | None:
    from .onepassword import resolve_secret
    from .storage import read_json

    ensure_local_no_proxy()
    value = account.browser.proxy
    if account.browser.proxy_from_meta or account.browser.resolved_proxy_meta:
        path = account.browser.resolved_proxy_meta or account.profile / "meta.json"
        value = read_json(path).get("proxy")
        if not value:
            raise WorkspaceError("旧 meta.json 缺少代理；拒绝静默改为直连")
    elif account.browser.proxy_ref:
        value = resolve_secret(
            account.browser.proxy_ref,
            account.browser.resolved_proxy_op_account or account.credentials.get("op_account"),
        )
    return parse_proxy(value) if value else None
