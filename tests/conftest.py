import pytest


@pytest.fixture(autouse=True)
def _isolated_global_settings(tmp_path_factory, monkeypatch):
    """Never read or write the user's real ~/.recaper.toml from tests."""
    monkeypatch.setenv("RECAPER_SETTINGS", str(tmp_path_factory.mktemp("settings") / "recaper.toml"))


@pytest.fixture(autouse=True)
def _offline_references(request, monkeypatch):
    """Book tests never look the series up online (AniList / Fandom) unless a test opts in."""
    if "online" not in request.keywords:
        from recaper import webrefs

        monkeypatch.setattr(webrefs, "build", lambda *a, **k: None)
