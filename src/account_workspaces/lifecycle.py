"""One worker per account, profile locks, readiness, status and cooperative shutdown."""

import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import Account, Catalog
from .errors import WorkspaceError
from .storage import is_locked, read_json, write_json


def profile_lock(account: Account) -> Path:
    return account.profile / ".accountctl.lock"


def session_status(account: Account) -> dict:
    active = is_locked(profile_lock(account))
    path = account.profile / "session.json"
    status = read_json(path) if path.exists() else {}
    if not active:
        if status.get("state") == "failed":
            return {"state": "failed", "pid": None, "error": status.get("error")}
        return {"state": "stopped", "pid": None}
    return {
        "state": status.get("state", "starting"),
        "pid": status.get("pid"),
        "token": status.get("token"),
    }


def start_account(
    catalog: Catalog, account: Account, *, headless: bool = False, no_urls: bool = False
) -> dict:
    if is_locked(profile_lock(account)):
        raise WorkspaceError(f"{account.id} 已经运行或正在启动")
    token = uuid.uuid4().hex
    command = [
        sys.executable,
        "-m",
        "account_workspaces.worker",
        "--config",
        str(catalog.path),
        "--account",
        account.id,
        "--token",
        token,
    ]
    if headless:
        command.append("--headless")
    if no_urls:
        command.append("--no-urls")
    # Do not capture library output in logs: third-party exceptions can include secrets.
    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        path = account.profile / "session.json"
        status = read_json(path) if path.exists() else {}
        if status.get("token") == token:
            if status.get("state") == "running":
                return status
            if status.get("state") == "failed":
                raise WorkspaceError(status.get("error", "浏览器启动失败"))
        if child.poll() is not None:
            raise WorkspaceError("浏览器进程启动失败；请运行 accountctl doctor")
        time.sleep(0.1)
    # Don't kill a PID: it might be re-used, and browser shutdown should be graceful.
    write_json(account.profile / "stop.json", {"token": token})
    raise WorkspaceError("启动超时，已请求结束本次启动；请检查 status")


def stop_account(account: Account) -> bool:
    status = session_status(account)
    if status["state"] in {"stopped", "failed"}:
        return False
    if not status.get("token"):
        raise WorkspaceError("账号正在取得启动锁，请稍后重试")
    write_json(account.profile / "stop.json", {"token": status["token"]})
    return True


def stop_requested(account: Account, token: str) -> bool:
    path = account.profile / "stop.json"
    return path.exists() and read_json(path).get("token") == token
