import os

import pytest
from camoufox.ip import Proxy

from account_workspaces.errors import WorkspaceError
from account_workspaces.network import account_proxy, build_proxy_config, parse_proxy
from account_workspaces.storage import write_json


def test_original_camoufox_geoip_regression(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setenv("no_proxy", "other.example")
    proxy, no_proxy = build_proxy_config("http://user:pa%3Ass@127.0.0.1:8080")
    assert proxy == {
        "server": "http://127.0.0.1:8080",
        "username": "user",
        "password": "pa:ss",
    }
    assert "bypass" not in proxy
    Proxy(**proxy)
    assert "127.0.0.1" in no_proxy
    assert "example.com" in no_proxy and "other.example" in no_proxy
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"]


def test_ipv6():
    assert parse_proxy("socks5://[::1]:1080") == {"server": "socks5://[::1]:1080"}


@pytest.mark.parametrize(
    "proxy",
    [
        "http://localhost",
        "file:///etc/passwd",
        "http://localhost:bad",
        "http://host:12/path",
    ],
)
def test_bad_proxy(proxy):
    with pytest.raises(WorkspaceError):
        parse_proxy(proxy)


def test_legacy_proxy_is_identical_and_never_silently_goes_direct(catalog):
    from dataclasses import replace

    account = catalog.accounts[0]
    account = replace(
        account,
        browser=replace(
            account.browser,
            legacy=True,
            proxy_from_meta=True,
            geoip=True,
        ),
    )
    write_json(account.profile / "meta.json", {"proxy": "http://user:secret@localhost:7897"})
    assert account_proxy(account) == {
        "server": "http://localhost:7897",
        "username": "user",
        "password": "secret",
    }
    write_json(account.profile / "meta.json", {})
    with pytest.raises(WorkspaceError, match="拒绝静默"):
        account_proxy(account)
