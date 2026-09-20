from account_workspaces.cli import main
from account_workspaces.config import load_catalog


def test_cli_init_add_list_and_validate(tmp_path, capsys):
    path = tmp_path / "accounts.toml"
    prefix = ["--config", str(path)]
    assert main(prefix + ["init"]) == 0
    assert main(prefix + ["add", "my-google", "--url", "https://accounts.google.com/"]) == 0
    assert main(prefix + ["validate"]) == 0
    assert main(prefix + ["list", "--json"]) == 0
    assert "my-google" in capsys.readouterr().out
    assert len(load_catalog(path).accounts) == 1


def test_adopt_preserves_files(catalog, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    fp = b'{"original": true}'
    (legacy / "fingerprint.json").write_bytes(fp)
    meta = '{"window": "1280x800", "geoip": true, "proxy":"http://me:secret@localhost:7897"}'
    (legacy / "meta.json").write_text(meta)
    assert (
        main(
            [
                "--config",
                str(catalog.path),
                "adopt-profile",
                "legacy",
                str(legacy),
            ]
        )
        == 0
    )
    assert (legacy / "fingerprint.json").read_bytes() == fp
    assert not (legacy / "workspace.json").exists()
    adopted = load_catalog(catalog.path).accounts[-1]
    assert adopted.browser.window == (1280, 800)
    assert adopted.browser.geoip
    assert adopted.browser.legacy
    assert adopted.browser.proxy_from_meta
    assert "secret" not in catalog.path.read_text()
    assert (legacy / "meta.json").read_text() == meta


def test_password_proxy_never_echoed(catalog, capsys):
    assert (
        main(
            [
                "--config",
                str(catalog.path),
                "add",
                "x",
                "--proxy",
                "http://me:secret@host:80",
            ]
        )
        == 1
    )
    assert "secret" not in capsys.readouterr().err


def test_add_browser_options(catalog):
    assert (
        main(
            [
                "--config",
                str(catalog.path),
                "add",
                "proxy-source",
                "--proxy",
                "http://localhost:7897",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--config",
                str(catalog.path),
                "add",
                "claude03",
                "--url",
                "https://claude.ai/",
                "--window",
                "1280x800",
                "--geoip",
                "--proxy-from-account",
                "proxy-source",
            ]
        )
        == 0
    )
    added = load_catalog(catalog.path).accounts[-1]
    assert added.browser.window == (1280, 800)
    assert added.browser.geoip
    assert added.urls == ("https://claude.ai/",)
    # The reference resolves to the source's proxy without copying it into the row.
    assert added.browser.proxy == "http://localhost:7897"
    assert "proxy-source" in catalog.path.read_text()


def test_add_rejects_conflicting_proxy_sources(catalog):
    assert (
        main(
            [
                "--config",
                str(catalog.path),
                "add",
                "claude06",
                "--proxy",
                "http://localhost:7897",
                "--proxy-from-account",
                "alpha",
            ]
        )
        == 1
    )
    assert len(load_catalog(catalog.path).accounts) == 2


def test_add_rejects_geoip_with_timezone(catalog, capsys):
    assert (
        main(
            [
                "--config",
                str(catalog.path),
                "add",
                "claude04",
                "--geoip",
                "--timezone",
                "Asia/Shanghai",
            ]
        )
        == 1
    )
    assert len(load_catalog(catalog.path).accounts) == 2


def test_add_rejects_bad_window(catalog):
    assert main(["--config", str(catalog.path), "add", "claude05", "--window", "huge"]) == 1
    assert len(load_catalog(catalog.path).accounts) == 2
