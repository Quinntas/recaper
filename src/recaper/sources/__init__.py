"""Connector registry. Add a new source by implementing sources.base.Connector and registering it here."""

from __future__ import annotations

from functools import lru_cache

from .base import ChapterInfo, Connector, MangaInfo, Page

_REGISTRY = {
    "mangadex": "recaper.sources.mangadex:MangaDex",
    "archive": "recaper.sources.archive:InternetArchive",
    "peppercarrot": "recaper.sources.peppercarrot:PepperCarrot",
}


@lru_cache(maxsize=None)
def get_connector(source_id: str) -> Connector:
    if source_id not in _REGISTRY:
        raise KeyError(f"unknown source {source_id!r}; available: {', '.join(_REGISTRY)}")
    module, cls = _REGISTRY[source_id].split(":")
    import importlib

    return getattr(importlib.import_module(module), cls)()


def list_sources() -> list[dict]:
    return [get_connector(s).info() for s in _REGISTRY]


__all__ = ["ChapterInfo", "Connector", "MangaInfo", "Page", "get_connector", "list_sources"]
