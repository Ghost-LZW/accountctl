"""Guard the Camoufox patches applied by account_workspaces.browser_patch.

The patches pin the browser build (so CamoufoxFetcher cannot upgrade it out
from under our frozen fingerprints) and take the GeoIP download off the
rate-limited GitHub API. They live in gitignored venv code, so these tests
check both the patcher's logic and that the live venv is actually patched.
"""

import json
import shutil
from pathlib import Path

import pytest

from account_workspaces import browser_patch as patcher


def unpatch(source: str, edits) -> str:
    """Reconstruct the upstream source by undoing `edits`."""
    for _, anchor, replacement in edits:
        source = source.replace(replacement, anchor)
    assert not patcher.is_patched(source)
    return source


@pytest.fixture
def pristine_pkgman():
    return unpatch(Path(patcher.module_path("pkgman")).read_text(), patcher._EDITS)


@pytest.fixture
def pristine_locale():
    return unpatch(Path(patcher.module_path("locale")).read_text(), patcher._LOCALE_EDITS)


@pytest.mark.parametrize("target", ["pkgman", "locale"])
def test_every_target_is_patched_in_the_venv(target):
    """A venv rebuild silently drops the patches."""
    source = Path(patcher.module_path(target)).read_text()
    assert patcher.is_patched(source), "run: python -m account_workspaces.browser_patch"


def test_patch_is_idempotent(pristine_pkgman):
    once = patcher.patch_source(pristine_pkgman)
    assert patcher.is_patched(once)
    assert patcher.patch_source(once) == once


def test_patches_produce_valid_python(pristine_pkgman, pristine_locale):
    targets = (
        (pristine_pkgman, patcher._EDITS),
        (pristine_locale, patcher._LOCALE_EDITS),
    )
    for source, edits in targets:
        compile(patcher.patch_source(source, edits), "patched.py", "exec")


def test_patch_refuses_when_anchor_is_missing(pristine_pkgman):
    """An upstream restructure must fail loudly, not half-apply."""
    broken = pristine_pkgman.replace("        resp = requests.get(self.api_url, timeout=20)\n", "")
    with pytest.raises(patcher.PatchError, match="expected exactly 1"):
        patcher.patch_source(broken)


def test_browser_resolves_to_pinned_build_without_api(monkeypatch):
    from camoufox import pkgman

    def fail(*args, **kwargs):
        raise AssertionError("resolved the browser through the GitHub API despite the pin")

    monkeypatch.setattr(pkgman.requests, "get", fail)
    fetcher = pkgman.CamoufoxFetcher()
    assert fetcher.verstr == patcher.PINNED_BROWSER
    assert f"v{patcher.PINNED_BROWSER}/" in fetcher.url


def test_geoip_resolves_without_api(monkeypatch):
    """The rate-limited API is exhausted routinely on this network."""
    from camoufox import pkgman
    from camoufox.locale import MMDB_REPO, MaxMindDownloader

    def fail(*args, **kwargs):
        raise AssertionError("resolved the GeoIP database through the GitHub API")

    monkeypatch.setattr(pkgman.requests, "get", fail)
    assert MaxMindDownloader(MMDB_REPO).get_asset().endswith("/GeoLite2-City.mmdb")


def test_geoip_downloader_selects_the_city_database():
    """The original patch hijacked this shared path and broke GeoIP downloads."""
    from camoufox.locale import MMDB_REPO, MaxMindDownloader

    downloader = MaxMindDownloader(MMDB_REPO)
    names = ["GeoLite2-ASN.mmdb", "GeoLite2-City.mmdb", "GeoLite2-Country.mmdb"]
    assets = [{"name": n, "browser_download_url": f"https://example.test/{n}"} for n in names]
    downloader.pinned_releases = lambda: [{"prerelease": False, "assets": assets}]
    assert downloader.get_asset() == "https://example.test/GeoLite2-City.mmdb"


def test_pin_matches_the_installed_browser():
    """A pin that disagrees with the install would fail every manifest check."""
    from camoufox.pkgman import Version

    assert Version.from_path().full_string == patcher.PINNED_BROWSER


def test_ensure_patched_repairs_a_rebuilt_venv():
    """A pip install or fresh venv drops the edits; the next run must restore them."""
    targets = [(Path(patcher.module_path(n)), e) for n, e in patcher.TARGETS]
    originals = [p.read_text() for p, _ in targets]
    try:
        for (path, edits), text in zip(targets, originals, strict=True):
            path.write_text(unpatch(text, edits))
        patcher.ensure_patched()
        for path, _ in targets:
            assert patcher.is_patched(path.read_text()), f"{path.name} was not repaired"
    finally:
        for (path, _), text in zip(targets, originals, strict=True):
            path.write_text(text)


def test_ensure_browser_restores_an_evicted_cache(tmp_path, monkeypatch):
    """~/Library/Caches is reclaimable; losing it must not trigger a re-download."""
    install, vault = tmp_path / "install", tmp_path / "vault"
    monkeypatch.setattr(patcher, "_install_dir", lambda: install)
    monkeypatch.setattr(patcher, "_vault_dir", lambda: vault)

    version = {"version": "135.0.1", "release": "beta.24"}
    install.mkdir()
    (install / "version.json").write_text(json.dumps(version))
    (install / "payload.bin").write_bytes(b"pinned build")

    assert "已备份" in (patcher.ensure_browser() or "")
    assert patcher.ensure_browser() is None, "backup should not be redone every run"

    shutil.rmtree(install)
    assert "还原" in (patcher.ensure_browser() or "")
    assert (install / "payload.bin").read_bytes() == b"pinned build"


def test_ensure_browser_does_not_overwrite_a_matching_install(tmp_path, monkeypatch):
    """Never clobber a good install: it is what the frozen fingerprints hash."""
    install, vault = tmp_path / "install", tmp_path / "vault"
    monkeypatch.setattr(patcher, "_install_dir", lambda: install)
    monkeypatch.setattr(patcher, "_vault_dir", lambda: vault)
    version = json.dumps({"version": "135.0.1", "release": "beta.24"})
    for path, payload in ((install, b"live"), (vault, b"stale")):
        path.mkdir()
        (path / "version.json").write_text(version)
        (path / "payload.bin").write_bytes(payload)

    assert patcher.ensure_browser() is None
    assert (install / "payload.bin").read_bytes() == b"live"
