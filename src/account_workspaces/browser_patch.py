#!/usr/bin/env python3
"""Pin the Camoufox browser build, and resolve GeoIP without the GitHub API.

Applied automatically: every `accountctl` run calls `ensure_patched()` before
touching the browser, so a rebuilt venv or an evicted cache repairs itself
instead of failing.

Two edits, both applied to the camoufox package inside the venv.

1. Pin the browser (pkgman.py)
------------------------------
`camoufox.pkgman.CamoufoxFetcher` resolves the browser download by asking the
GitHub releases API for the *first supported* release. Upstream has since
published newer builds (152.0.4-beta.29 at the time of writing), so an
unpinned install would fetch a build newer than the one our frozen
fingerprints are bound to. `runtime_identity()` records the browser version
and the executable's SHA-256, and `verify_manifest()` refuses to launch on a
mismatch -- so an unpinned upgrade turns every existing account workspace into
an unstartable one. `CamoufoxFetcher.install()` also starts with
`shutil.rmtree(INSTALL_DIR)`, so it destroys the good install before
replacing it.

The pin is a property of the *browser* downloader only. The original local
edit changed the shared `GitHubDownloader.get_asset()` base method, which also
hijacked the GeoIP downloader: `MaxMindDownloader` searched the hardcoded
browser zips for a `*-City.mmdb` asset, never found one, and raised
`MissingRelease('Failed to find GeoIP database release asset')`.

2. Take GeoIP off the API (locale.py)
-------------------------------------
`GeoLite2-City.mmdb` ships in site-packages rather than in the wheel, so a
venv rebuild deletes it and the next geoip lookup re-downloads it. That
download went through the rate-limited releases API, which is routinely
exhausted on this shared network. GitHub's `releases/latest/download/`
redirect serves the same asset without spending quota, so prefer it and keep
the API as a fallback. Upstream 0.5.x made the same move.

The edits are kept here rather than in the venv because `venv/` is gitignored:
any `pip install` or venv rebuild drops them and silently re-arms both
problems.

Usage
-----
Normally nothing to run -- `accountctl` applies this itself. To drive it by
hand:

    python -m account_workspaces.browser_patch            # apply (idempotent)
    python -m account_workspaces.browser_patch --check    # verify only

Run it with the interpreter whose camoufox you mean to patch -- it patches the
camoufox importable from `sys.path`, and refuses any version other than the
pinned one so that pointing it at an unrelated install fails loudly.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

# Single source of truth for the browser build. Must match the "browser" field
# recorded in each profile's workspace.json by fingerprint.runtime_identity().
PINNED_BROWSER = "135.0.1-beta.24"

# The camoufox release this patch's anchors were written against. A different
# version may have restructured pkgman.py, so refuse rather than guess.
EXPECTED_CAMOUFOX = "0.4.11"

# Present in patched files only; makes re-applying a no-op.
MARKER = "account-workspaces:pinned-browser"

_CONSTANTS_ANCHOR = "LOCAL_DATA: Path = Path(os.path.abspath(__file__)).parent\n"

_CONSTANTS = f"""
# {MARKER} -- see account_workspaces/browser_patch.py for why this is pinned.
# Only the browser is pinned; every other downloader (notably the GeoIP
# database) still resolves through the GitHub API.
PINNED_BROWSER_VERSION: str = {PINNED_BROWSER!r}
PINNED_BROWSER_ASSETS: frozenset = frozenset(
    {{
        'camoufox-{PINNED_BROWSER}-mac.arm64.zip',
        'camoufox-{PINNED_BROWSER}-mac.x86_64.zip',
        'camoufox-{PINNED_BROWSER}-win.x86_64.zip',
    }}
)
"""

_GET_ASSET_ANCHOR = '''    def get_asset(self) -> Any:
        """
        Fetch the latest release from the GitHub API.
        Gets the first asset that returns a truthy value from check_asset.
        """
        resp = requests.get(self.api_url, timeout=20)
        resp.raise_for_status()

        releases = resp.json()
'''

_GET_ASSET = '''    def pinned_releases(self) -> List[Dict]:
        """
        Releases to use instead of querying the GitHub API.

        Lets a subclass pin known assets so its download target cannot drift.
        An empty list means "ask the API".
        """
        return []

    def get_asset(self) -> Any:
        """
        Fetch the latest release from the GitHub API.
        Gets the first asset that returns a truthy value from check_asset.
        """
        releases = self.pinned_releases()

        if not releases:
            resp = requests.get(self.api_url, timeout=20)
            resp.raise_for_status()
            releases = resp.json()
'''

_FETCHER_ANCHOR = "        self.fetch_latest()\n"

_FETCHER = '''        self.fetch_latest()

    def pinned_releases(self) -> List[Dict]:
        """
        Pin the browser build so it cannot drift away from the version our
        frozen fingerprints are bound to. Platforms we do not pin fall back
        to the API.
        """
        name = f'camoufox-{PINNED_BROWSER_VERSION}-{OS_NAME}.{self.arch}.zip'
        if name not in PINNED_BROWSER_ASSETS:
            return []
        return [
            {
                'prerelease': False,
                'assets': [
                    {
                        'name': name,
                        'browser_download_url': (
                            'https://github.com/daijro/camoufox/releases/download/'
                            f'v{PINNED_BROWSER_VERSION}/{name}'
                        ),
                    }
                ],
            }
        ]
'''

_EDITS = (
    ("module constants", _CONSTANTS_ANCHOR, _CONSTANTS_ANCHOR + _CONSTANTS),
    ("GitHubDownloader.get_asset", _GET_ASSET_ANCHOR, _GET_ASSET),
    ("CamoufoxFetcher.pinned_releases", _FETCHER_ANCHOR, _FETCHER),
)

# --- locale.py: resolve the GeoIP database without the API ------------------
#
# GeoLite2-City.mmdb lives in site-packages, i.e. inside the gitignored venv,
# so a venv rebuild deletes it and re-arms the download. The download used the
# GitHub releases API, which is rate limited per source IP and is routinely
# exhausted on this (shared) network. GitHub's `releases/latest/download/`
# redirect serves the same asset without touching the API, so use it directly
# and keep the API only as a fallback. Upstream 0.5.x made the same move.

_MMDB_ANCHOR = """    def missing_asset_error(self) -> None:
        raise MissingRelease('Failed to find GeoIP database release asset')
"""

_MMDB = '''    def missing_asset_error(self) -> None:
        raise MissingRelease('Failed to find GeoIP database release asset')

    def pinned_releases(self) -> List[Dict]:
        """
        account-workspaces:pinned-browser -- see account_workspaces/browser_patch.py.

        Resolve the database through GitHub's `latest/download` redirect, which
        serves the newest release without spending the rate-limited API quota.
        """
        name = 'GeoLite2-City.mmdb'
        return [
            {
                'prerelease': False,
                'assets': [
                    {
                        'name': name,
                        'browser_download_url': (
                            f'https://github.com/{self.github_repo}/releases/'
                            f'latest/download/{name}'
                        ),
                    }
                ],
            }
        ]
'''

_LOCALE_EDITS = (("MaxMindDownloader.pinned_releases", _MMDB_ANCHOR, _MMDB),)


class PatchError(RuntimeError):
    """The patch could not be applied, and nothing was written."""


def is_patched(source: str) -> bool:
    return MARKER in source


def patch_source(source: str, edits=_EDITS) -> str:
    """Return `source` with `edits` applied.

    Raises PatchError if any anchor is missing or ambiguous, so an upstream
    restructure fails loudly instead of half-applying.
    """
    if is_patched(source):
        return source
    for label, anchor, replacement in edits:
        found = source.count(anchor)
        if found != 1:
            raise PatchError(
                f"anchor for {label} matched {found} times, expected exactly 1; "
                f"camoufox {EXPECTED_CAMOUFOX} may have been restructured"
            )
        source = source.replace(anchor, replacement)
    return source


def module_path(name: str) -> Path:
    """Locate a camoufox submodule importable from sys.path."""
    spec = importlib.util.find_spec(f"camoufox.{name}")
    if spec is None or not spec.origin:
        raise PatchError(
            f"no camoufox is importable by {sys.executable}; "
            "run this with the project venv's interpreter"
        )
    return Path(spec.origin)


def find_pkgman() -> Path:
    return module_path("pkgman")


# Each target file, with the edits that patch it.
TARGETS = (("pkgman", _EDITS), ("locale", _LOCALE_EDITS))


def check_version() -> None:
    """Refuse any camoufox other than the one this patch was written against.

    Reported by import path, not just name: an unrelated camoufox on the
    default interpreter would fetch a newer browser and wipe the install our
    frozen fingerprints depend on.
    """
    from importlib.metadata import PackageNotFoundError, version

    path = find_pkgman()
    try:
        found = version("camoufox")
    except PackageNotFoundError:
        raise PatchError(
            f"camoufox at {path.parent} has no package metadata, so its version "
            f"cannot be confirmed. This patch targets {EXPECTED_CAMOUFOX} in the "
            "project venv; run it with venv/bin/python."
        ) from None
    if found != EXPECTED_CAMOUFOX:
        raise PatchError(
            f"expected camoufox {EXPECTED_CAMOUFOX} but found {found} at "
            f"{path.parent}. This patch is written against {EXPECTED_CAMOUFOX}; "
            "run it with the project venv's interpreter."
        )


def apply() -> list[str]:
    """Apply any missing edits. Returns the names of files actually changed."""
    check_version()
    changed = []
    for name, edits in TARGETS:
        path = module_path(name)
        source = path.read_text()
        if is_patched(source):
            continue
        path.write_text(patch_source(source, edits))
        changed.append(path.name)
    return changed


def _install_dir() -> Path:
    from camoufox.pkgman import INSTALL_DIR

    return Path(INSTALL_DIR)


def _vault_dir() -> Path:
    """Where the pinned browser is kept safe from cache eviction."""
    from platformdirs import user_data_dir

    return Path(user_data_dir("account-workspaces")) / "browser" / PINNED_BROWSER


def _installed_version(path: Path) -> str | None:
    try:
        import orjson

        data = orjson.loads((path / "version.json").read_bytes())
        return f"{data['version']}-{data['release']}"
    except Exception:
        return None


def ensure_browser() -> str | None:
    """Keep a copy of the pinned browser outside the OS cache, and restore it.

    Camoufox installs under `~/Library/Caches`, which macOS and cleanup tools
    are free to purge -- and that happened here on 2026-09-21. A missing
    install is not a small problem: the next launch re-downloads whatever
    upstream calls current, which matches no existing frozen fingerprint.

    Restoring from a local copy also avoids re-downloading ~600MB, and cannot
    silently substitute a different build the way a fresh fetch would.

    Returns a message when it did something, else None.
    """
    install, vault = _install_dir(), _vault_dir()

    if _installed_version(install) == PINNED_BROWSER:
        if _installed_version(vault) != PINNED_BROWSER:
            vault.parent.mkdir(parents=True, exist_ok=True)
            # Stage first: a copy interrupted midway must not look complete.
            staging = vault.with_name(f"{vault.name}.partial")
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(install, staging, symlinks=True)
            shutil.rmtree(vault, ignore_errors=True)
            staging.rename(vault)
            return f"已备份钉定浏览器到 {vault}"
        return None

    if _installed_version(vault) != PINNED_BROWSER:
        return None  # Nothing to restore from; let the caller's own checks speak.

    shutil.rmtree(install, ignore_errors=True)
    install.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(vault, install, symlinks=True)
    return f"浏览器缓存已丢失，已从本地副本还原 {PINNED_BROWSER}"


def ensure_patched() -> None:
    """Repair the venv's camoufox in place, before anything launches a browser.

    Called on every `accountctl` run: the edits live in gitignored venv code,
    so a `pip install` or a rebuilt venv silently drops them. Self-healing
    here means the pin cannot quietly lapse and take every frozen fingerprint
    with it. The browser install is guarded the same way, since it sits in a
    cache directory the OS may reclaim.

    Deliberately quiet on success and non-fatal on failure -- if camoufox is
    missing or restructured, the commands that actually need a browser fail on
    their own with a better message than this could give.
    """
    try:
        if changed := apply():
            print(
                f"已自动修复 camoufox 补丁（{', '.join(changed)}）：浏览器锁定 {PINNED_BROWSER}",
                file=sys.stderr,
            )
        if note := ensure_browser():
            print(note, file=sys.stderr)
    except (PatchError, OSError, ImportError):
        return


def report_browser_drift() -> str | None:
    """Warn if the installed browser differs from the pinned build."""
    try:
        from camoufox.pkgman import Version

        found = Version.from_path().full_string
    except Exception:
        return "installed browser version is unreadable; run this before creating workspaces"
    if found != PINNED_BROWSER:
        return (
            f"installed browser is {found} but the pin is {PINNED_BROWSER}; "
            "existing workspaces expect the pinned build"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the patch is applied without writing anything",
    )
    args = parser.parse_args(argv)

    try:
        check_version()
        unpatched = []
        for name, edits in TARGETS:
            path = module_path(name)
            source = path.read_text()
            if is_patched(source):
                print(f"already patched: {path.name}")
                continue
            if args.check:
                unpatched.append(path)
                continue
            path.write_text(patch_source(source, edits))
            print(f"patched: {path.name}")

        if unpatched:
            for path in unpatched:
                print(f"UNPATCHED {path}", file=sys.stderr)
            print("run: python -m account_workspaces.browser_patch", file=sys.stderr)
            return 1
        print(f"browser pinned to {PINNED_BROWSER}; GeoIP resolves without the GitHub API")
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if warning := report_browser_drift():
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
