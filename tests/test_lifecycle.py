import subprocess
import sys
from unittest.mock import Mock

import pytest

from account_workspaces.errors import WorkspaceError
from account_workspaces.lifecycle import (
    profile_lock,
    session_status,
    start_account,
    stop_account,
    stop_requested,
)
from account_workspaces.storage import is_locked, lock, write_json


def test_os_lock_and_stale_files(catalog):
    account = catalog.accounts[0]
    path = profile_lock(account)
    with lock(path):
        assert is_locked(path)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from account_workspaces.storage import is_locked; "
                "import sys; sys.exit(0 if is_locked(Path(sys.argv[1])) else 1)",
                str(path),
            ],
            check=False,
        )
        assert result.returncode == 0
    assert not is_locked(path)
    write_json(account.profile / "session.json", {"state": "running", "pid": 123})
    assert session_status(account)["state"] == "stopped"


def test_duplicate_start_blocked(catalog, monkeypatch):
    account = catalog.accounts[0]
    spawn = Mock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with lock(profile_lock(account)), pytest.raises(WorkspaceError, match="已经运行"):
        start_account(catalog, account)
    spawn.assert_not_called()


def test_stop_is_token_scoped_not_pid_kill(catalog):
    account = catalog.accounts[0]
    with lock(profile_lock(account)):
        write_json(
            account.profile / "session.json",
            {
                "state": "running",
                "pid": 123,
                "token": "current",
            },
        )
        assert stop_account(account)
        assert stop_requested(account, "current")
        assert not stop_requested(account, "old")
    assert not stop_account(account)


def test_failed_state_is_preserved_without_treating_it_as_running(catalog):
    account = catalog.accounts[0]
    write_json(
        account.profile / "session.json",
        {
            "state": "failed",
            "pid": None,
            "error": "safe message",
        },
    )
    assert session_status(account) == {"state": "failed", "pid": None, "error": "safe message"}
    assert not stop_account(account)


def test_no_urls_is_passed_to_the_worker_without_touching_config(catalog, monkeypatch):
    """--no-urls is per-run: the worker is told to skip, the catalog is unchanged."""
    account = catalog.accounts[0]
    # poll() returning an exit code makes start_account give up immediately.
    spawn = Mock(return_value=Mock(poll=Mock(return_value=1)))
    monkeypatch.setattr(subprocess, "Popen", spawn)

    for no_urls, expected in ((False, False), (True, True)):
        spawn.reset_mock()
        with pytest.raises(WorkspaceError, match="进程启动失败"):
            start_account(catalog, account, no_urls=no_urls)
        assert ("--no-urls" in spawn.call_args.args[0]) is expected

    assert catalog.accounts[0].urls == account.urls
