"""Login-gate scoring, the cheap substitute for a real sign-in attempt."""

import pytest

from account_workspaces import probe
from account_workspaces.config import Account, BrowserSettings


@pytest.fixture
def account(tmp_path):
    return Account(
        id="a",
        label="A",
        provider="custom",
        tags=(),
        urls=("about:blank",),
        profile=tmp_path / "profiles" / "a",
        enabled=True,
        browser=BrowserSettings(),
    )


def test_login_findings_flag_every_gate():
    result = {"observed": {"login": {"cloudflare": True, "challenge": True, "status": 400}}}
    assert len(probe.login_findings(result)) == 3


def test_login_findings_empty_on_a_clean_pass():
    result = {"observed": {"login": {"cloudflare": False, "challenge": False, "status": 200}}}
    assert probe.login_findings(result) == []


def test_findings_catches_the_generic_renderer_and_dnt():
    from account_workspaces.egress import Egress

    identity = Egress(
        ip="1.1.1.1",
        country="US",
        timezone="America/Los_Angeles",
        latitude=1.0,
        longitude=1.0,
        locale="en-US",
    )
    observed = probe.findings(
        {
            "timezone": "America/Los_Angeles",
            "language": "en-US",
            "webdriver": False,
            "webglUnmaskedRenderer": "Apple M1, or similar",
            "doNotTrack": "1",
            "globalPrivacyControl": True,
            "canvasStable": True,
            "ua": "Mozilla/5.0 Firefox/152.0",
        },
        identity,
    )
    assert any("or similar" in item for item in observed)
    assert any("doNotTrack" in item for item in observed)
    assert any("globalPrivacyControl" in item for item in observed)
