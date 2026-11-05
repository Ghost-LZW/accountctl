from dataclasses import replace

import pytest

from account_workspaces import egress
from account_workspaces.egress import (
    Egress,
    apply,
    compare,
    identity_from_config,
    parse_payload,
    prefs,
    remember,
    resolve,
)
from account_workspaces.errors import WorkspaceError

EXIT = {
    "ip": "198.51.100.7",
    "country": "US",
    "loc": "25.7617,-80.1918",
    "timezone": "America/New_York",
}
OTHER = {**EXIT, "ip": "203.0.113.9"}


def fake(monkeypatch, answers):
    calls = []

    def get(url, proxy):
        calls.append((url, proxy))
        value = answers.get(url) if isinstance(answers, dict) else answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(egress, "_get", get)
    return calls


@pytest.fixture(autouse=True)
def located(monkeypatch):
    monkeypatch.setattr(
        egress,
        "_locate",
        lambda ip: ("America/New_York", 25.7617, -80.1918, "US"),
    )
    monkeypatch.setattr(egress, "_primary_locale", lambda country: "en-US")


def account(catalog, **browser):
    return replace(catalog.accounts[0], browser=replace(catalog.accounts[0].browser, **browser))


def test_parses_both_provider_shapes():
    assert parse_payload(EXIT)["latitude"] == 25.7617
    assert (
        parse_payload({"ip": EXIT["ip"], "country_code": "us", "latitude": 1, "longitude": 2})[
            "country"
        ]
        == "US"
    )
    with pytest.raises(ValueError):
        parse_payload({"ip": "not-an-ip", "country": "US", "latitude": 0, "longitude": 0})


def test_parses_cloudflare_trace():
    payload = egress._payload("fl=abc\nip=198.51.100.7\nloc=US\ncolo=MIA\nts=1\n")
    parsed = parse_payload(payload)
    assert parsed["country"] == "US"
    assert parsed["latitude"] is None


def test_bare_ip_probe_uses_the_local_database_for_country(catalog, monkeypatch):
    fake(monkeypatch, ["198.51.100.7\n"])
    identity = resolve(account(catalog, probe_url="https://api.ipify.org"), None)
    assert identity.country == "US"
    assert identity.timezone == "America/New_York"


def test_custom_probe_replaces_the_defaults(catalog, monkeypatch):
    calls = fake(monkeypatch, [EXIT])
    identity = resolve(account(catalog, probe_url="https://www.google.com/cdn-cgi/trace"), None)
    assert identity.country == "US"
    assert [call[0] for call in calls] == ["https://www.google.com/cdn-cgi/trace"]


def test_split_exit_is_refused(catalog, monkeypatch):
    fake(monkeypatch, {egress.IPINFO: EXIT, egress.IPAPI: OTHER})
    with pytest.raises(WorkspaceError, match="出口不同"):
        resolve(account(catalog), None)


def test_dead_endpoints_are_refused(catalog, monkeypatch):
    fake(monkeypatch, [ValueError(), ValueError()])
    with pytest.raises(WorkspaceError, match="无法确认"):
        resolve(account(catalog), None)


def test_resolves_exit_with_proxy_and_pins_are_verified(catalog, monkeypatch):
    calls = fake(monkeypatch, [EXIT, EXIT])
    proxy = {"server": "http://127.0.0.1:7897", "username": "u", "password": "p"}
    identity = resolve(account(catalog, locale="en-US", timezone="America/New_York"), proxy)
    assert identity.country == "US"
    assert identity.locale == "en-US"
    assert identity.timezone == "America/New_York"
    assert identity.latitude == 25.7617
    assert all(call[1] == proxy for call in calls)


def test_wrong_pin_is_rejected(catalog, monkeypatch):
    fake(monkeypatch, [EXIT, EXIT])
    with pytest.raises(WorkspaceError, match="相差过大"):
        resolve(account(catalog, timezone="Asia/Shanghai"), None)
    fake(monkeypatch, [EXIT, EXIT])
    with pytest.raises(WorkspaceError, match="不常用"):
        resolve(account(catalog, locale="zh-CN"), None)


def test_stored_locale_survives_same_country(catalog, monkeypatch):
    subject = account(catalog)
    remember(
        subject,
        Egress("198.51.100.7", "US", "America/New_York", 25.7, -80.1, "es-US"),
    )
    fake(monkeypatch, [EXIT, EXIT])
    monkeypatch.setattr(egress, "_primary_locale", lambda country: "en-US")
    assert resolve(subject, None).locale == "es-US"


def test_strict_refuses_a_country_move(catalog, monkeypatch):
    subject = account(catalog, egress="strict")
    remember(
        subject,
        Egress("198.51.100.7", "DE", "Europe/Berlin", 52.5, 13.4, "de-DE"),
    )
    fake(monkeypatch, [EXIT, EXIT])
    with pytest.raises(WorkspaceError, match="DE 变为 US"):
        resolve(subject, None)


def test_apply_replaces_stale_location_and_spoofs_webrtc():
    identity = Egress("198.51.100.7", "US", "America/New_York", 25.7, -80.1, "en-US")
    config = {
        "timezone": "Asia/Shanghai",
        "locale:language": "zh",
        "locale:region": "CN",
        "geolocation:latitude": 1.0,
        "fonts:spacing_seed": 9,
    }
    result = apply(config, identity, "spoof")
    assert result["timezone"] == "America/New_York"
    assert result["locale:region"] == "US"
    assert result["locale:all"] == "en-US"
    assert result["geolocation:latitude"] == 25.7
    assert result["webrtc:ipv4"] == "198.51.100.7"
    assert result["fonts:spacing_seed"] == 9
    assert config["timezone"] == "Asia/Shanghai", "input config must not be mutated"


def test_off_identity_keeps_frozen_geolocation():
    identity = Egress("0.0.0.0", "FR", "Europe/Paris", 0.0, 0.0, "fr-FR")
    result = apply({"geolocation:latitude": 48.8}, identity, "keep")
    assert result["timezone"] == "Europe/Paris"
    assert "geolocation:latitude" not in result


def test_apply_uses_ipv6_when_the_exit_is_v6():
    identity = Egress("2001:db8::1", "US", "America/New_York", 25.7, -80.1, "en-US")
    assert apply({}, identity, "spoof")["webrtc:ipv6"] == "2001:db8::1"


def test_leak_prefs_always_close_os_signals():
    identity = Egress("198.51.100.7", "US", "America/New_York", 25.7, -80.1, "en-US")
    for policy in ("spoof", "block", "keep"):
        values = prefs(identity, policy)
        assert values["geo.provider.use_corelocation"] is False
        assert values["geo.provider.use_geoclue"] is False
        assert values["network.proxy.socks_remote_dns"] is True
        assert values["network.trr.mode"] == 0
        assert values["javascript.use_us_english_locale"] is False
        assert values["intl.accept_languages"] == "en-US"
    assert prefs(identity, "block")["media.peerconnection.enabled"] is False
    assert prefs(identity, "spoof")["network.dns.disableIPv6"] is True


def test_off_identity_only_carries_configured_values(catalog):
    subject = account(catalog, egress="off", locale="fr-FR", timezone="Europe/Paris")
    identity = identity_from_config(subject)
    assert identity.timezone == "Europe/Paris"
    assert identity.locale == "fr-FR"
    assert identity_from_config(account(catalog, egress="off")) is None


def test_compare_reports_every_mismatch():
    identity = Egress("198.51.100.7", "US", "America/New_York", 25.7, -80.1, "en-US")
    assert compare(identity, {"timezone": "America/New_York", "language": "en-US"}) == []
    problems = compare(
        identity,
        {"timezone": "UTC", "language": "zh-CN", "coords": [50.0, 10.0]},
    )
    assert len(problems) == 3


def test_probe_url_rejects_credentials(catalog):
    from account_workspaces.config import parse_catalog

    raw = {
        "version": 1,
        "accounts": [{"id": "a", "browser": {"probe_url": "https://user:pw@x.test/i"}}],
    }
    with pytest.raises(WorkspaceError, match="不能包含凭据"):
        parse_catalog(catalog.path, raw)
