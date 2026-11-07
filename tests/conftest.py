import pytest

from account_workspaces.config import add_accounts, initialize


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "accounts.toml"
    initialize(path)
    return add_accounts(
        path,
        [
            {
                "id": "alpha",
                "tags": ["google", "work"],
                "urls": ["about:blank"],
                "browser": {"engine": "camoufox"},
            },
            {
                "id": "beta",
                "tags": ["google"],
                "urls": ["about:blank"],
                "browser": {"engine": "camoufox"},
            },
        ],
    )
