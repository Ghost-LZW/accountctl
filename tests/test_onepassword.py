import json
import subprocess
from unittest.mock import Mock

import pytest

from account_workspaces.errors import WorkspaceError
from account_workspaces.onepassword import OnePassword, import_rows, resolve_secret


def item(item_id="item123", url="https://accounts.google.com/login?token=secret"):
    return {
        "id": item_id,
        "vault": {"id": "vault123"},
        "title": "Google",
        "urls": [{"href": url}],
    }


def test_import_only_metadata_strict_domain_and_dedup(catalog):
    rows = import_rows(
        [
            item(),
            item(),
            item("evil", "https://google.com.evil.example/"),
            item("other", "https://notgoogle.com/"),
        ],
        catalog,
        domain="google.com",
        prefix="google",
        op_account="personal",
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "google-item123"
    assert rows[0]["urls"] == ["https://accounts.google.com/"]
    assert rows[0]["credentials"]["password_ref"] == "op://vault123/item123/password"
    assert "secret" not in json.dumps(rows)


def test_reimport_does_not_overwrite(catalog):
    from account_workspaces.config import add_accounts

    options = {"domain": "google.com", "prefix": "google", "op_account": None}
    updated = add_accounts(catalog.path, import_rows([item()], catalog, **options))
    assert import_rows([item()], updated, **options) == []


def test_cli_reads_only_list_metadata(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([item()]), ""))
    monkeypatch.setattr(subprocess, "run", run)
    assert OnePassword().list_items("Private", None) == [item()]
    assert run.call_args.args[0] == [
        "op",
        "item",
        "list",
        "--categories",
        "Login",
        "--format",
        "json",
        "--vault",
        "Private",
    ]
    assert run.call_args.kwargs["timeout"] == 90


def test_secret_stays_in_memory_and_errors_redacted(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "value")
    assert resolve_secret("env://TEST_SECRET") == "value"
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess([], 1, "", "password=secret"),
        ),
    )
    with pytest.raises(WorkspaceError) as error:
        OnePassword().read("op://v/i/password")
    assert "secret" not in str(error.value)


def test_op_timeout_and_missing_executable(monkeypatch):
    for exception in (FileNotFoundError(), subprocess.TimeoutExpired("op", 90)):
        monkeypatch.setattr(subprocess, "run", Mock(side_effect=exception))
        with pytest.raises(WorkspaceError):
            OnePassword().list_items(None, None)
