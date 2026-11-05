from dataclasses import dataclass, replace
from unittest.mock import Mock

import pytest

from account_workspaces.errors import WorkspaceError
from account_workspaces.fingerprint import (
    browser_environment,
    browser_options,
    digest,
    installed_executable,
    prepare_fingerprint,
    restore_dataclass,
    settings_identity,
    verify_manifest,
)
from account_workspaces.storage import read_json, write_json


def manifest_for(account):
    source = {"input": "fixed"}
    write_json(account.profile / "fingerprint.json", source)
    effective = {"config": {"fonts:spacing_seed": 42}, "prefs": {}}
    return {
        "schema": 1,
        "account_id": account.id,
        "runtime": {"camoufox": "test"},
        "settings": settings_identity(account),
        "effective": effective,
        "fingerprint_id": digest(effective),
        "source_sha256": digest(source),
    }


def test_restore_never_calls_random_generator(catalog, monkeypatch):
    account = replace(
        catalog.accounts[0], browser=replace(catalog.accounts[0].browser, egress="off")
    )
    manifest = manifest_for(account)
    write_json(account.profile / "workspace.json", manifest)
    monkeypatch.setattr(
        "account_workspaces.fingerprint.runtime_identity", lambda: manifest["runtime"]
    )
    random_generator = Mock(side_effect=AssertionError("Must not generate"))
    monkeypatch.setattr("camoufox.utils.launch_options", random_generator)
    assert prepare_fingerprint(account) == manifest
    assert prepare_fingerprint(account) == manifest
    random_generator.assert_not_called()


def test_detects_changes_and_account_copy(catalog):
    account, other = catalog.accounts
    manifest = manifest_for(account)
    verify_manifest(account, manifest, manifest["runtime"])
    with pytest.raises(WorkspaceError, match="其他账号"):
        verify_manifest(replace(other, profile=account.profile), manifest, manifest["runtime"])
    with pytest.raises(WorkspaceError, match="漂移"):
        verify_manifest(account, manifest, {"camoufox": "new"})
    with pytest.raises(WorkspaceError, match="不一致"):
        verify_manifest(
            replace(account, browser=replace(account.browser, window=(1600, 1000))),
            manifest,
            manifest["runtime"],
        )
    manifest["effective"]["config"]["fonts:spacing_seed"] = 43
    with pytest.raises(WorkspaceError, match="校验"):
        verify_manifest(account, manifest, manifest["runtime"])


def test_lost_source_never_replaced(catalog):
    account = catalog.accounts[0]
    manifest = manifest_for(account)
    (account.profile / "fingerprint.json").unlink()
    with pytest.raises(WorkspaceError, match="丢失"):
        verify_manifest(account, manifest, manifest["runtime"])


def test_missing_manifest_of_managed_profile_fails(catalog, monkeypatch):
    account = catalog.accounts[0]
    write_json(account.profile / ".managed", {})
    monkeypatch.setattr("account_workspaces.fingerprint.runtime_identity", lambda: {})
    with pytest.raises(WorkspaceError, match="丢失"):
        prepare_fingerprint(account)


def test_restores_optional_nested_dataclass():
    @dataclass
    class Inner:
        value: int

    @dataclass
    class Outer:
        inner: Inner | None

    assert restore_dataclass(Outer, {"inner": {"value": 3}}) == Outer(Inner(3))


def test_child_environment_excludes_secrets_and_injected_config(monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "secret")
    monkeypatch.setenv("MY_PROXY_PASSWORD", "secret")
    monkeypatch.setenv("CAMOU_CONFIG_1", "evil")
    result = browser_environment({"CAMOU_CONFIG_1": "fixed"})
    assert result["CAMOU_CONFIG_1"] == "fixed"
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in result
    assert "MY_PROXY_PASSWORD" not in result


def test_frozen_timezone_reaches_browser_via_tz(catalog, monkeypatch):
    """Camoufox's `timezone` config key alone is ignored by the bundled build."""
    account = catalog.accounts[0]
    manifest = manifest_for(account)
    manifest["effective"]["config"]["timezone"] = "America/New_York"
    monkeypatch.setattr(
        "account_workspaces.fingerprint.installed_executable", lambda: "/nonexistent/camoufox"
    )
    options = browser_options(account, manifest, headless=True, proxy=None)
    assert options["env"]["TZ"] == "America/New_York"


def test_no_timezone_leaves_tz_unset(catalog, monkeypatch):
    account = catalog.accounts[0]
    monkeypatch.setattr(
        "account_workspaces.fingerprint.installed_executable", lambda: "/nonexistent/camoufox"
    )
    options = browser_options(account, manifest_for(account), headless=True, proxy=None)
    assert "TZ" not in options["env"]


def test_schema_one_is_migrated_and_location_is_released(catalog, monkeypatch):
    account = catalog.accounts[0]
    manifest = manifest_for(account)
    manifest["effective"]["config"].update(
        {"timezone": "UTC", "locale:language": "en", "locale:region": "US", "seed": 1}
    )
    manifest["fingerprint_id"] = digest(manifest["effective"])
    write_json(account.profile / "workspace.json", manifest)
    monkeypatch.setattr(
        "account_workspaces.fingerprint.runtime_identity", lambda: manifest["runtime"]
    )
    migrated = prepare_fingerprint(account)
    assert migrated["schema"] == 2
    assert set(migrated["effective"]["config"]) == {"seed", "fonts:spacing_seed"}
    assert migrated["fingerprint_id"] == digest(migrated["effective"])
    assert read_json(account.profile / "workspace.json")["schema"] == 2
    # The location is no longer part of what locks the profile.
    changed = replace(account, browser=replace(account.browser, locale="de-DE"))
    verify_manifest(changed, migrated, manifest["runtime"])
    with pytest.raises(WorkspaceError, match="不一致"):
        verify_manifest(
            replace(changed, browser=replace(changed.browser, window=(900, 900))),
            migrated,
            manifest["runtime"],
        )


def test_off_policy_keeps_the_schema_one_manifest(catalog, monkeypatch):
    account = replace(
        catalog.accounts[0], browser=replace(catalog.accounts[0].browser, egress="off")
    )
    manifest = manifest_for(account)
    write_json(account.profile / "workspace.json", manifest)
    monkeypatch.setattr(
        "account_workspaces.fingerprint.runtime_identity", lambda: manifest["runtime"]
    )
    assert prepare_fingerprint(account) == manifest


def test_identity_reaches_env_and_prefs(catalog, monkeypatch):
    from account_workspaces.egress import Egress

    account = catalog.accounts[0]
    identity = Egress("198.51.100.7", "US", "America/New_York", 25.7, -80.1, "en-US")
    monkeypatch.setattr(
        "account_workspaces.fingerprint.installed_executable", lambda: "/nonexistent/camoufox"
    )
    options = browser_options(
        account,
        manifest_for(account),
        headless=True,
        proxy=None,
        identity=identity,
    )
    assert options["env"]["TZ"] == "America/New_York"
    assert options["env"]["LANG"] == "en-US.UTF-8"
    assert options["env"]["LC_ALL"] == "en-US.UTF-8"
    assert options["firefox_user_prefs"]["intl.accept_languages"] == "en-US"
    assert options["firefox_user_prefs"]["geo.provider.use_corelocation"] is False
    assert options["firefox_user_prefs"]["media.peerconnection.enabled"] is True


def test_state_write_is_private(catalog):
    manifest_for(catalog.accounts[0])
    assert read_json(catalog.accounts[0].profile / "fingerprint.json") == {"input": "fixed"}


def test_missing_browser_does_not_auto_download(monkeypatch):
    get_path = Mock(side_effect=FileNotFoundError)
    monkeypatch.setattr("camoufox.pkgman.camoufox_path", get_path)
    with pytest.raises(WorkspaceError, match="未安装"):
        installed_executable()
    get_path.assert_called_once_with(download_if_missing=False)


def test_legacy_generation_keeps_geoip_proxy_and_freezes_once(catalog, monkeypatch):
    from browserforge.fingerprints import FingerprintGenerator

    account = catalog.accounts[0]
    account = replace(
        account,
        browser=replace(
            account.browser,
            legacy=True,
            window=(1280, 800),
            geoip=True,
        ),
    )
    original = FingerprintGenerator(browser="firefox").generate().dumps()
    account.profile.mkdir(parents=True)
    source = account.profile / "fingerprint.json"
    source.write_text(original)
    proxy = {"server": "http://localhost:7897", "username": "user", "password": "secret"}
    monkeypatch.setattr(
        "account_workspaces.fingerprint.runtime_identity", lambda: {"version": "test"}
    )

    def generate(**kwargs):
        assert kwargs["proxy"] == proxy
        # The location is derived per launch, not sampled here.
        assert kwargs["geoip"] is False
        assert kwargs["locale"] is None
        assert kwargs["block_webrtc"] is False
        assert kwargs["window"] == (1280, 800)
        assert kwargs["exclude_addons"] == []
        kwargs["config"].update({"fonts:spacing_seed": 7})
        return {"proxy": proxy}

    generator = Mock(side_effect=generate)
    monkeypatch.setattr("camoufox.utils.launch_options", generator)
    first = prepare_fingerprint(account, proxy=proxy)
    second = prepare_fingerprint(account, proxy=proxy)
    assert first == second
    generator.assert_called_once()
    assert source.read_text() == original
    assert first["schema"] == 2
    assert first["effective"]["config"]["fonts:spacing_seed"] == 7
    assert "secret" not in (account.profile / "workspace.json").read_text()
