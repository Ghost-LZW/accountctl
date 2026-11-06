"""Chrome engine: identity freezing, runtime pinning and launch options."""

import pytest

from account_workspaces import chrome
from account_workspaces.config import BrowserSettings, Catalog, Account
from account_workspaces.errors import WorkspaceError


@pytest.fixture
def account(tmp_path):
    profile = tmp_path / "profiles" / "a"
    return Account(
        id="a",
        label="A",
        provider="custom",
        tags=(),
        urls=("about:blank",),
        profile=profile,
        enabled=True,
        browser=BrowserSettings(window=(1280, 800), engine="chrome"),
    )


@pytest.fixture(autouse=True)
def fake_chrome(monkeypatch):
    executable = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    monkeypatch.setattr(chrome, "installed_executable", lambda account=None: __import__("pathlib").Path(executable))
    monkeypatch.setattr(chrome, "version_of", lambda executable: "141.0.7390.55")


def test_runtime_pins_the_major_version(account):
    first = chrome.runtime_identity(account)
    assert first["major"] == "141"
    assert first["engine"] == "chrome"


def test_prepare_freezes_only_the_window(account):
    manifest = chrome.prepare(account)
    assert manifest["engine"] == "chrome"
    assert manifest["effective"] == {"config": {"window": [1280, 800]}, "prefs": {}}


def test_prepare_refuses_a_camoufox_profile(account):
    account.profile.mkdir(parents=True)
    (account.profile / "workspace.json").write_text(
        '{"schema": 2, "engine": "camoufox", "effective": {}}', encoding="utf-8"
    )
    with pytest.raises(WorkspaceError, match="camoufox"):
        chrome.prepare(account)


def test_verify_rejects_a_new_major_version(account):
    manifest = chrome.prepare(account)
    newer = dict(manifest["runtime"], browser="142.0.1.1", major="142")
    with pytest.raises(WorkspaceError, match="大版本"):
        chrome.verify(account, manifest, newer)


def test_options_carry_location_and_webrtc_policy(account):
    from account_workspaces.egress import Egress

    identity = Egress(
        ip="69.17.3.250",
        country="US",
        timezone="America/Los_Angeles",
        latitude=34.0,
        longitude=-118.0,
        locale="en-US",
    )
    args = chrome._args(
        account, identity, proxy={"server": "http://127.0.0.1:7901"}, headless=False
    )
    assert "--lang=en-US" in args
    assert "--proxy-server=http://127.0.0.1:7901" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert f"--user-data-dir={account.profile / 'user_data'}" in args


def test_off_identity_emits_no_location(account):
    args = chrome._args(account, None, proxy=None, headless=True)
    assert not any(a.startswith("--lang=") for a in args)
    assert "--headless=new" in args


def test_webrtc_keep_drops_the_policy_flag(account):
    from dataclasses import replace

    keep = replace(account, browser=replace(account.browser, webrtc="keep"))
    args = chrome._args(keep, None, proxy=None, headless=False)
    assert not any("webrtc-ip-handling" in a for a in args)
