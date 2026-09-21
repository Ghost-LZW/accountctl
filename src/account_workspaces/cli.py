"""Thin command-line interface over the account catalog and workspace lifecycle."""

import argparse
import json
import shutil
import sys
import tomllib
from pathlib import Path

from . import __version__
from .browser_patch import ensure_patched
from .config import (
    add_accounts,
    default_config,
    initialize,
    load_catalog,
    parse_catalog,
)
from .errors import WorkspaceError
from .fingerprint import MANIFEST, runtime_identity, verify_manifest
from .lifecycle import session_status, start_account, stop_account
from .onepassword import OnePassword, import_rows
from .storage import read_json


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="accountctl",
        description="Camoufox 多账号管理：独立会话、稳定且不同的指纹",
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", type=Path, default=None, help="账号 TOML 路径")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建空白本机配置，不覆盖已有文件")
    commands.add_parser("validate", help="校验配置，不访问 1Password 或启动浏览器")
    commands.add_parser("doctor", help="检查运行时、指纹和 op 安装情况，不读取密码")
    listing = commands.add_parser("list", help="列出账号及运行状态")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--tag", action="append", default=[])
    add = commands.add_parser("add", help="新增任意网站账号")
    add.add_argument("id")
    add.add_argument("--label")
    add.add_argument("--provider", default="custom")
    add.add_argument("--url", action="append", help="可重复")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--username-ref")
    add.add_argument("--password-ref")
    add.add_argument("--op-account")
    add.add_argument("--proxy")
    add.add_argument("--proxy-ref")
    add.add_argument("--proxy-from-account", help="复用另一账号的代理来源，不复制密码")
    add.add_argument("--window", help="例如 1280x800")
    add.add_argument("--geoip", action="store_true", help="首次启动按代理出口 IP 固化地理参数")
    add.add_argument("--locale", help="与 --geoip 互斥")
    add.add_argument("--timezone", help="与 --geoip 互斥")
    for action in ("start", "stop", "status"):
        sub = commands.add_parser(
            action,
            help={
                "start": "后台启动，终端退出后浏览器仍保留",
                "stop": "请求正常关闭指定账号并保存登录数据",
                "status": "查看账号状态",
            }[action],
        )
        sub.add_argument("ids", nargs="*")
        sub.add_argument("--tag", action="append", default=[])
        sub.add_argument("--all", action="store_true")
        if action == "start":
            sub.add_argument("--headless", action="store_true", help="测试用；仍需 stop 关闭")
            sub.add_argument(
                "--no-urls",
                action="store_true",
                help="本次只开空白页，不打开账号配置的网址；不修改配置",
            )
    importing = commands.add_parser("import-1password", help="批量导入 Login 条目引用")
    importing.add_argument("--vault", help="建议限定保险库")
    importing.add_argument("--tag", help="1Password 条目标签")
    importing.add_argument("--domain", help="例如 google.com；匹配该域及其子域")
    importing.add_argument("--prefix", default="op")
    importing.add_argument("--op-account", help="有多个 1Password 账号时指定")
    importing.add_argument("--apply", action="store_true", help="写入；默认仅预览")
    migration = commands.add_parser("adopt-profile", help="登记旧 Camoufox profile，不搬动数据")
    migration.add_argument("id")
    migration.add_argument("directory", type=Path)
    migration.add_argument("--url", action="append", default=["about:blank"])
    migration.add_argument("--proxy-ref", help="替代旧 meta.json 中的带密码代理")
    migration.add_argument("--proxy-from-account", help="旧 meta.json 没有代理时，复用另一账号的")
    migration.add_argument("--locale")
    migration.add_argument("--timezone")
    return result


def doctor(catalog) -> int:
    failures = 0
    runtime = None
    try:
        runtime = runtime_identity()
        print(f"OK Camoufox {runtime['camoufox']} / browser {runtime['browser']}")
    except Exception as error:
        print(str(error) if isinstance(error, WorkspaceError) else "FAIL Camoufox 运行时不可用")
        failures += 1
    print("OK 1Password CLI 已安装" if shutil.which("op") else "INFO 未安装 op（仅影响凭据集成）")
    seen = set()
    for account in catalog.accounts:
        path = account.profile / MANIFEST
        try:
            if path.exists() and runtime:
                manifest = read_json(path)
                verify_manifest(account, manifest, runtime)
                if manifest["fingerprint_id"] in seen:
                    raise WorkspaceError("检测到重复的最终指纹")
                seen.add(manifest["fingerprint_id"])
                print(f"OK {account.id} 指纹 {manifest['fingerprint_id'][:16]}")
            elif (account.profile / "user_data").exists():
                if not (account.profile / "fingerprint.json").exists():
                    raise WorkspaceError("已有会话缺少指纹")
                print(f"INFO {account.id} 旧 profile：首次启动将固化剩余随机参数")
            else:
                print(f"INFO {account.id} 尚未初始化")
        except WorkspaceError as error:
            print(f"FAIL {account.id}: {error}")
            failures += 1
    return 1 if failures else 0


def _window(value: str) -> list[int]:
    try:
        width, height = (int(n) for n in value.lower().split("x"))
    except (ValueError, AttributeError):
        raise WorkspaceError("窗口格式必须是 宽x高，例如 1280x800") from None
    return [width, height]


def execute(args) -> int:
    path = (args.config or default_config()).expanduser().resolve()
    if args.command == "init":
        initialize(path)
        print(f"已创建 {path}")
        return 0
    catalog = load_catalog(path)
    if args.command == "validate":
        print(f"配置有效，共 {len(catalog.accounts)} 个账号")
    elif args.command == "doctor":
        return doctor(catalog)
    elif args.command == "add":
        row = {
            "id": args.id,
            "label": args.label or args.id,
            "provider": args.provider,
            "tags": args.tag,
        }
        if args.url:
            row["urls"] = args.url
        credentials = {
            name: getattr(args, name)
            for name in ("username_ref", "password_ref", "op_account")
            if getattr(args, name)
        }
        browser = {
            name: getattr(args, name)
            for name in ("proxy", "proxy_ref", "proxy_from_account", "locale", "timezone")
            if getattr(args, name)
        }
        if args.geoip:
            browser["geoip"] = True
        if args.window:
            browser["window"] = _window(args.window)
        if credentials:
            row["credentials"] = credentials
        if browser:
            row["browser"] = browser
        add_accounts(path, [row])
        print(f"已添加 {args.id}；使用 accountctl start {args.id} 启动")
    elif args.command == "list":
        rows = [
            {
                "id": a.id,
                "label": a.label,
                "provider": a.provider,
                "tags": list(a.tags),
                "enabled": a.enabled,
                **session_status(a),
            }
            for a in catalog.accounts
            if set(args.tag) <= set(a.tags)
        ]
        for row in rows:
            row.pop("token", None)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print("ID\t名称\t状态\t标签")
            for row in rows:
                state = row["state"] if row["enabled"] else "disabled"
                print(f"{row['id']}\t{row['label']}\t{state}\t{','.join(row['tags'])}")
    elif args.command in {"start", "stop", "status"}:
        # Disabled accounts can still be inspected/stopped after a config edit.
        if args.command != "start":
            from dataclasses import replace

            catalog = replace(
                catalog,
                accounts=tuple(replace(a, enabled=True) for a in catalog.accounts),
            )
        accounts = catalog.select(args.ids, args.tag, args.all)
        failures = 0
        for account in accounts:
            try:
                if args.command == "start":
                    status = start_account(
                        catalog, account, headless=args.headless, no_urls=args.no_urls
                    )
                    print(f"{account.id}: running · 指纹 {status['fingerprint_id'][:16]}")
                elif args.command == "stop":
                    requested = stop_account(account)
                    print(f"{account.id}: {'已请求正常关闭' if requested else 'stopped'}")
                else:
                    status = session_status(account)
                    print(f"{account.id}: {status['state']}")
                    if status.get("error"):
                        print(f"  {status['error']}")
            except WorkspaceError as error:
                failures += 1
                print(f"{account.id}: {error}", file=sys.stderr)
        return 1 if failures else 0
    elif args.command == "import-1password":
        items = OnePassword(args.op_account).list_items(args.vault, args.tag)
        rows = import_rows(
            items,
            catalog,
            domain=args.domain,
            prefix=args.prefix,
            op_account=args.op_account,
        )
        # Validate the complete candidate before displaying or writing anything.
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("accounts", []).extend(rows)
        parse_catalog(path, raw)
        for row in rows:
            print(f"{row['id']}\t{row['label']}")
        if args.apply and rows:
            add_accounts(path, rows)
            print(f"已导入 {len(rows)} 个账号引用（未读取密码）")
        else:
            print(f"待新增 {len(rows)} 个账号；使用 --apply 写入。已有引用会跳过。")
    elif args.command == "adopt-profile":
        directory = args.directory.expanduser().resolve()
        if not (directory / "fingerprint.json").is_file():
            raise WorkspaceError("旧 profile 必须包含 fingerprint.json")
        if (directory / MANIFEST).exists():
            raise WorkspaceError("此目录已经由 accountctl 管理")
        meta = read_json(directory / "meta.json") if (directory / "meta.json").exists() else {}
        browser = {"legacy": True, "geoip": meta.get("geoip", False)}
        if args.locale:
            browser["locale"] = args.locale
        if args.timezone:
            browser["timezone"] = args.timezone
        if meta.get("window"):
            browser["window"] = _window(meta["window"])
        if args.proxy_ref:
            browser["proxy_ref"] = args.proxy_ref
        elif args.proxy_from_account:
            browser["proxy_from_account"] = args.proxy_from_account
        elif meta.get("proxy"):
            browser["proxy_from_meta"] = True
        add_accounts(
            path,
            [
                {
                    "id": args.id,
                    "profile": str(directory),
                    "browser": browser,
                    "urls": args.url,
                }
            ],
        )
        print("已登记旧目录；fingerprint.json 和 user_data 未修改。")
        print("旧脚本没有保存 Canvas/WebGL 等最终参数；首次启动将固定它们，不能还原历史值。")
        if browser.get("proxy_from_meta"):
            print("继续使用旧 meta.json 中的代理，未将代理密码复制到新配置。")
        if meta.get("geoip"):
            print("保留 GeoIP：首次通过原代理解析地理位置/时区/语言后固化，之后不重新采样。")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        # The pin lives in gitignored venv code; repair it before it matters.
        ensure_patched()
        return execute(args)
    except WorkspaceError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except OSError:
        print("错误：本地文件或进程操作失败；请检查路径与权限", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已中断；已启动的账号可通过 status / stop 管理", file=sys.stderr)
        return 130
