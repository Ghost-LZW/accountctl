import os
from pathlib import Path

import pytest

from account_workspaces.config import (
    add_accounts,
    initialize,
    load_catalog,
    parse_catalog,
    validate_reference,
)
from account_workspaces.errors import WorkspaceError


def test_paths_are_relative_to_config_not_cwd(catalog, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = load_catalog(catalog.path)
    assert loaded.accounts[0].profile == catalog.path.parent / ".account-workspaces/profiles/alpha"


def test_selection(catalog):
    assert [a.id for a in catalog.select([], ["google", "work"], False)] == ["alpha"]
    assert [a.id for a in catalog.select(["beta", "alpha", "beta"], [], False)] == ["beta", "alpha"]
    for args in [([], [], False), (["no"], [], False), (["alpha"], [], True)]:
        with pytest.raises(WorkspaceError):
            catalog.select(*args)


def test_atomic_add_preserves_comments_and_failed_update(catalog):
    before = catalog.path.read_text()
    with pytest.raises(WorkspaceError, match="重复"):
        add_accounts(catalog.path, [{"id": "alpha"}])
    assert catalog.path.read_text() == before
    add_accounts(catalog.path, [{"id": "gamma", "credentials": {"password_ref": "env://PASS"}}])
    assert "# 本机账号目录" in catalog.path.read_text()
    assert len(load_catalog(catalog.path).accounts) == 3
    if os.name != "nt":
        assert catalog.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "row",
    [
        {"id": "../escape"},
        {"id": "X"},
        {"id": "a", "password": "secret"},
        {"id": "a", "browser": {"engine": "chrome"}},
        {"id": "a", "browser": {"geoip": True, "locale": "en-US"}},
        {"id": "a", "browser": {"window": [True, 900]}},
        {"id": "a", "browser": {"proxy": "http://me:secret@localhost:80"}},
        {"id": "a", "credentials": {"password_ref": "literal-secret"}},
        {"id": "a", "urls": ["javascript:alert(1)"]},
        {"id": "a", "urls": ["https://me:secret@example.com"]},
        {"id": "a", "urls": []},
        {"id": "a", "enabled": "false"},
        {"id": "a", "label": "terminal\x1b[31m"},
        {"id": "a", "browser": {"timezone": "../etc/passwd"}},
        {"id": "a", "browser": {"locale": "not_a_language"}},
    ],
)
def test_invalid_rows_fail_without_echoing_secrets(tmp_path, row):
    with pytest.raises(WorkspaceError) as error:
        parse_catalog(tmp_path / "c.toml", {"version": 1, "accounts": [row]})
    assert "secret" not in str(error.value)


def test_shared_or_nested_profiles_rejected(tmp_path):
    for second in ("same", "same/nested"):
        with pytest.raises(WorkspaceError, match="共享"):
            parse_catalog(
                tmp_path / "c.toml",
                {
                    "version": 1,
                    "accounts": [{"id": "a", "profile": "same"}, {"id": "b", "profile": second}],
                },
            )


def test_default_profile_symlinks_cannot_bypass_isolation(tmp_path):
    profiles = tmp_path / ".account-workspaces/profiles"
    profiles.mkdir(parents=True)
    (profiles / "alpha").mkdir()
    (profiles / "beta").symlink_to(profiles / "alpha", target_is_directory=True)
    with pytest.raises(WorkspaceError, match="共享"):
        parse_catalog(
            tmp_path / "c.toml",
            {
                "version": 1,
                "accounts": [{"id": "alpha"}, {"id": "beta"}],
            },
        )


def test_init_never_overwrites(catalog):
    before = catalog.path.read_bytes()
    with pytest.raises(WorkspaceError):
        initialize(catalog.path)
    assert catalog.path.read_bytes() == before


def test_proxy_override_replaces_inherited_reference(tmp_path):
    result = parse_catalog(
        tmp_path / "c.toml",
        {
            "version": 1,
            "defaults": {"browser": {"proxy_ref": "env://PROXY"}},
            "accounts": [{"id": "a", "browser": {"proxy": "http://localhost:80"}}],
        },
    )
    assert result.accounts[0].browser.proxy_ref is None


def test_example_is_valid():
    assert len(load_catalog(Path(__file__).parents[1] / "accounts.example.toml").accounts) == 3


def test_reuse_proxy_without_reusing_identity(tmp_path):
    catalog = parse_catalog(
        tmp_path / "c.toml",
        {
            "version": 1,
            "accounts": [
                {
                    "id": "old",
                    "profile": "legacy",
                    "browser": {
                        "legacy": True,
                        "proxy_from_meta": True,
                    },
                },
                {"id": "new", "browser": {"proxy_from_account": "old"}},
                {"id": "third", "browser": {"proxy_from_account": "new"}},
            ],
        },
    )
    old, new, third = catalog.accounts
    assert new.browser.resolved_proxy_meta == old.profile / "meta.json"
    assert third.browser.resolved_proxy_meta == new.browser.resolved_proxy_meta
    assert not new.browser.legacy
    assert old.profile != new.profile


@pytest.mark.parametrize("source", ["absent", "self"])
def test_proxy_reuse_rejects_invalid_reference(tmp_path, source):
    with pytest.raises(WorkspaceError, match="循环或"):
        parse_catalog(
            tmp_path / "c.toml",
            {
                "version": 1,
                "accounts": [
                    {"id": "self", "browser": {"proxy_from_account": source}},
                ],
            },
        )


@pytest.mark.parametrize(
    "reference",
    [
        "op://vault/item/password",
        "op://vault/item/section/password",
        "env://MY_PASSWORD",
    ],
)
def test_secret_reference_validation(reference):
    assert validate_reference(reference) == reference
