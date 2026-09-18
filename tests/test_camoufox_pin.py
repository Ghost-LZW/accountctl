"""Guard the Camoufox patches applied by scripts/patch_camoufox.py.

The patches pin the browser build (so CamoufoxFetcher cannot upgrade it out
from under our frozen fingerprints) and take the GeoIP download off the
rate-limited GitHub API. They live in gitignored venv code, so these tests
check both the patcher's logic and that the live venv is actually patched.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load_patcher():
    spec = importlib.util.spec_from_file_location("patch_camoufox", SCRIPTS / "patch_camoufox.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["patch_camoufox"] = module
    spec.loader.exec_module(module)
    return module


patcher = load_patcher()


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
    assert patcher.is_patched(source), "run: python scripts/patch_camoufox.py"


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
