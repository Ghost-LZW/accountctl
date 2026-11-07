"""Shard engine: identity freezing and the location override."""

import pytest

from account_workspaces import shard
from account_workspaces.config import Account, BrowserSettings
from account_workspaces.errors import WorkspaceError


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
        browser=BrowserSettings(engine="shard"),
    )


class FakeProfile:
    def __init__(self, config):
        self.config = dict(config)
        self.id = "p1"
        self.noise = ()

    def set_noise(self, *vectors):
        clone = FakeProfile(self.config)
        clone.noise = vectors
        return clone

    def with_override(self, **overrides):
        clone = FakeProfile({**self.config, **overrides})
        clone.noise = self.noise
        clone.id = self.id
        return clone


class FakeSDK:
    def create_profile(self, platform=None):
        return FakeProfile({"name": "mac-m2-mba13", "timezone": "Europe/Warsaw"})

    def open_profile(self, id):
        return FakeProfile({"name": "mac-m2-mba13", "timezone": "Europe/Warsaw"})


def test_prepare_freezes_the_template(account, monkeypatch):
    monkeypatch.setattr(shard, "_sdk", lambda account: FakeSDK())
    manifest = shard.prepare(account)
    assert manifest["engine"] == "shard"
    assert manifest["effective"]["config"]["template"] == "mac-m2-mba13"


def test_prepare_refuses_an_engine_switch(account, monkeypatch, tmp_path):
    monkeypatch.setattr(shard, "_sdk", lambda account: FakeSDK())
    account.profile.mkdir(parents=True)
    (account.profile / "workspace.json").write_text('{"schema": 2, "engine": "chrome"}', encoding="utf-8")
    with pytest.raises(WorkspaceError, match="shard"):
        shard.prepare(account)


def test_override_pins_location_and_turns_noise_on(account):
    from account_workspaces.egress import Egress

    identity = Egress(
        ip="69.17.3.250",
        country="US",
        timezone="America/Los_Angeles",
        latitude=34.0,
        longitude=-118.0,
        locale="en-US",
    )
    profile = shard._override(FakeProfile({"name": "t"}), identity)
    assert profile.config["timezone"] == "America/Los_Angeles"
    assert profile.config["icu_locale"] == "en-US"
    assert profile.config["navigator"]["language"] == "en-US"
    assert set(profile.noise) == {"canvas", "audio", "client_rects", "fonts"}


def test_runtime_identity_names_the_engine():
    runtime = shard.runtime_identity()
    assert runtime["engine"] == "shard"
    assert runtime["shardx"]
