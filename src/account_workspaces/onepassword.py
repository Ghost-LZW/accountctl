"""1Password metadata discovery and lazy secret resolution; never export vaults."""

import json
import os
import subprocess
from urllib.parse import urlsplit

from .config import Catalog, validate_reference
from .errors import WorkspaceError


class OnePassword:
    def __init__(self, account: str | None = None):
        self.account = account

    def _run(self, args: list[str]) -> str:
        command = ["op", *args]
        if self.account:
            command += ["--account", self.account]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except FileNotFoundError:
            raise WorkspaceError("找不到 op；请安装 1Password CLI 并启用桌面 App 集成") from None
        except subprocess.TimeoutExpired:
            raise WorkspaceError("1Password 请求超时；请解锁桌面 App 后重试") from None
        if result.returncode:
            # op errors may echo arguments or other sensitive material.
            raise WorkspaceError("1Password 请求失败；请检查解锁状态、账号和保险库权限")
        return result.stdout

    def list_items(self, vault: str | None, tag: str | None) -> list[dict]:
        args = ["item", "list", "--categories", "Login", "--format", "json"]
        if vault:
            args += ["--vault", vault]
        if tag:
            args += ["--tags", tag]
        try:
            items = json.loads(self._run(args))
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError
            return items
        except ValueError:
            raise WorkspaceError("1Password 返回了无效的条目列表") from None

    def read(self, reference: str) -> str:
        validate_reference(reference)
        return self._run(["read", reference, "--no-newline"])


def resolve_secret(reference: str, account: str | None = None) -> str:
    validate_reference(reference)
    if reference.startswith("env://"):
        value = os.getenv(reference[6:])
        if not value:
            raise WorkspaceError("所引用的环境变量未设置或为空")
        return value
    return OnePassword(account).read(reference)


def import_rows(
    items: list[dict],
    catalog: Catalog,
    *,
    domain: str | None,
    prefix: str,
    op_account: str | None,
) -> list[dict]:
    if domain:
        domain = domain.lower().strip().rstrip(".")
        if not domain or any(c in domain for c in "/:@ ?#"):
            raise WorkspaceError("--domain 必须是域名，例如 google.com")
    existing_refs = {
        (a.credentials.get("op_account"), a.credentials.get("username_ref"))
        for a in catalog.accounts
    }
    rows = []
    for item in items:
        try:
            item_id, vault_id = item["id"], item["vault"]["id"]
            if not all(
                isinstance(value, str) and value.isalnum() and value.isascii()
                for value in (item_id, vault_id)
            ):
                raise ValueError
            urls = []
            for entry in item.get("urls", []):
                href = entry.get("href", "")
                parsed = urlsplit(href)
                host = (parsed.hostname or "").lower().rstrip(".")
                if (
                    parsed.scheme in {"https", "http"}
                    and host
                    and not parsed.username
                    and not parsed.password
                    and (not domain or host == domain or host.endswith("." + domain))
                ):
                    # Strip path/query/fragment: saved login URLs may contain tokens.
                    urls.append(f"{parsed.scheme}://{parsed.netloc}/")
            if domain and not urls:
                continue
            base = f"op://{vault_id}/{item_id}"
            identity = (op_account, f"{base}/username")
            if identity in existing_refs:
                continue
            credentials = {
                "username_ref": f"{base}/username",
                "password_ref": f"{base}/password",
            }
            if op_account:
                credentials["op_account"] = op_account
            rows.append(
                {
                    "id": f"{prefix}-{item_id.lower()}",
                    "label": item.get("title", item_id),
                    "provider": domain or "custom",
                    "tags": ["1password", *([domain] if domain else [])],
                    "urls": list(dict.fromkeys(urls)) or ["about:blank"],
                    "credentials": credentials,
                }
            )
            existing_refs.add(identity)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise WorkspaceError("1Password 条目元数据格式错误；未写入配置") from None
    return rows
