import pytest


@pytest.fixture(autouse=True)
def _isolated_global_settings(tmp_path_factory, monkeypatch):
    """Never read or write the user's real ~/.recaper.toml from tests."""
    monkeypatch.setenv("RECAPER_SETTINGS", str(tmp_path_factory.mktemp("settings") / "recaper.toml"))
