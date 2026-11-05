import json
from pathlib import Path

import numpy as np
import pytest

from recaper.config import Config, parse_chapter_ranges
from recaper.ingest import detect_source, discover
from recaper.llm import Chain, Provider, QuotaError, extract_json, make_chain
from recaper.llm.opencode import _text_from_events
from recaper.panels import detect_manga_panels, extract_chapter, imread, slice_manhwa
from recaper.render.styles import BlurFillStyle, MotionStyle, PanelInfo, Shot, build_shots
from recaper.story import Bible, _resolve_beats

from fixtures import make_manga, make_manhwa


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    make_manga(root / "manga")
    make_manhwa(root / "manhwa")
    return root


# ---------------------------------------------------------------- ingest/panels


def test_discover_and_detect(samples):
    manga = discover(samples / "manga", samples / "manga" / ".recaper")
    manhwa = discover(samples / "manhwa", samples / "manhwa" / ".recaper")
    assert [c.title for c in manga] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert detect_source(manga) == "manga"
    assert detect_source(manhwa) == "manhwa"


def test_manga_panels_rtl_order(samples):
    page = imread(samples / "manga" / "Chapter 1" / "002.png")  # top, bottom-right, bottom-left
    panels = detect_manga_panels(page, rtl=True)
    assert len(panels) == 3
    # second panel (bottom-right) must come before bottom-left: check by mean x of non-white ink
    assert panels[0].shape[1] > panels[1].shape[1]  # top panel is full width


def test_manhwa_slicing_ignores_gradient_bands(samples):
    pages = sorted((samples / "manhwa" / "Episode 1").glob("*.jpg"))
    assert len(slice_manhwa(pages)) == 8  # 8 drawn panels, cut across 1600px chunks


def test_extract_chapter_is_cached(samples, tmp_path):
    ch = discover(samples / "manga", tmp_path)[0]
    first = extract_chapter(ch, "manga", True, tmp_path)
    again = extract_chapter(ch, "manga", True, tmp_path)
    assert [p.id for p in first] == [p.id for p in again]
    assert all((tmp_path / p.file).exists() for p in first)


# ------------------------------------------------------------------- config


def test_chapter_ranges():
    assert parse_chapter_ranges("1-3,7", 10) == [0, 1, 2, 6]
    assert parse_chapter_ranges("8-", 10) == [7, 8, 9]
    assert parse_chapter_ranges(None, 3) == [0, 1, 2]


def test_config_overrides_and_aliases(tmp_path):
    (tmp_path / "recaper.toml").write_text('lang = "pt"\n[tts]\nengine = "edge"\n', "utf-8")
    cfg = Config.load(tmp_path, {"writer": "deepseek-free,claude", "render.fps": 60})
    assert cfg.lang == "pt" and cfg.tts.engine == "edge" and cfg.render.fps == 60
    assert cfg.writer == "opencode:opencode/deepseek-v4.1-flash-free,claude:sonnet"


# ---------------------------------------------------------------------- llm


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_text_only_provider_cannot_read():
    with pytest.raises(ValueError):
        make_chain("deepseek-free", "reader", need_vision=True)
    chain = make_chain("opencode:opencode/deepseek-v4.1-flash-free,claude:sonnet", "reader", need_vision=True)
    assert [p.name for p in chain.providers] == ["claude"]


class _Fake(Provider):
    def __init__(self, name, reply=None, exc=None):
        super().__init__(None)
        self.name, self.reply, self.exc, self.calls = name, reply, exc, 0

    def complete(self, system, parts):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.reply


def test_chain_falls_back_on_quota_and_remembers():
    a = _Fake("a", exc=QuotaError("free limit"))
    b = _Fake("b", reply='{"ok": true}')
    chain = Chain([a, b], "writer")
    assert chain.complete_json("s", ["x"]) == {"ok": True}
    assert chain.complete_json("s", ["x"]) == {"ok": True}
    assert a.calls == 1  # skipped after the quota error


def test_opencode_event_parsing():
    lines = [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"id": "p1", "type": "text", "text": '{"be'}},
        {"type": "text", "part": {"id": "p1", "type": "text", "text": '{"beats": []}'}},
    ]
    assert _text_from_events("\n".join(json.dumps(x) for x in lines)) == '{"beats": []}'


# --------------------------------------------------------------------- story


def test_bible_merges_aliases():
    b = Bible()
    b.merge([{"name": "Sung Jin-Woo", "aliases": ["Jinwoo"], "desc": "E-rank"}], None)
    b.merge([{"name": "jinwoo", "desc": "now stronger"}], [{"term": "Gate", "meaning": "portal"}])
    assert list(b.characters) == ["Sung Jin-Woo"]
    assert b.characters["Sung Jin-Woo"]["desc"] == "now stronger"
    assert "Gate: portal" in b.text()


def test_resolve_beats_cleans_and_fills_panels():
    labels = {"P1": "c1-p1", "P2": "c1-p2"}
    beats = _resolve_beats(
        [{"text": "**Hello** world", "panels": ["P9"]}, {"text": "Next", "panels": ["p2", "P1", "P2"]}, {"text": " ", "panels": ["P1"]}],
        lambda lab: labels.get(lab.upper()),
    )
    assert beats[0] == {"text": "Hello world", "panels": ["c1-p2"], "emphasis": "normal"}
    assert beats[1]["panels"] == ["c1-p2", "c1-p1"]
    assert len(beats) == 2


# -------------------------------------------------------------------- render


def _panels(tmp_path):
    import cv2

    infos = {}
    for pid, (w, h) in {"a": (800, 600), "b": (600, 900), "tall": (720, 3000)}.items():
        img = np.full((h, w, 3), 128, np.uint8)
        cv2.putText(img, pid, (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 4, (0, 0, 0), 8)
        path = tmp_path / f"{pid}.jpg"
        cv2.imwrite(str(path), img)
        infos[pid] = PanelInfo(str(path), w, h)
    return infos


@pytest.mark.parametrize("style_cls", [MotionStyle, BlurFillStyle])
def test_styles_render_frames(tmp_path, style_cls):
    infos = _panels(tmp_path)
    style = style_cls(640, 360, infos)
    prev = Shot(0, 2, ["a"], "normal", 0)
    for shot in (Shot(2, 4, ["b"] if style_cls is BlurFillStyle else ["a", "b"], "hit", 1), Shot(4, 7, ["tall"], "reveal", 2)):
        for t in (0.0, 0.1, 0.5, shot.dur - 0.01):
            frame = style.frame(shot, prev, t)
            assert frame.shape == (360, 640, 3) and frame.dtype == np.uint8
        prev = shot


def test_motion_layout_fits_canvas(tmp_path):
    style = MotionStyle(1920, 1080, _panels(tmp_path))
    for pids in (["a"], ["b"], ["tall"], ["a", "b"], ["a", "b", "tall"]):
        for r in style.layout(pids).values():
            assert r.x >= 0 and r.y >= 0 and r.x + r.w <= 1920 + 1 and r.y + r.h <= 1080 + 1


def test_build_shots_splits_beats_for_blurfill():
    beats = [{"start": 0.0, "end": 6.0, "panels": ["a", "b", "c"], "emphasis": "hit"}]
    shots = build_shots(beats, "blurfill")
    assert [s.panels for s in shots] == [["a"], ["b"], ["c"]]
    assert shots[0].emphasis == "hit" and shots[1].emphasis == "normal"
    assert shots[-1].end == 6.0
    assert len(build_shots(beats, "motion")) == 1


# ------------------------------------------------------------------ pipeline


class _ScriptedModel(Provider):
    """Fake model answering reader / bible / writer / hook prompts with valid JSON."""

    name = "fake"

    def __init__(self):
        super().__init__(None)
        import threading

        self.lock, self.calls = threading.Lock(), []

    def complete(self, system, parts):
        import re as _re

        with self.lock:
            self.calls.append(system[:40])
        text = "\n".join(p for p in parts if isinstance(p, str))
        if "meticulous manga" in system:
            labels = _re.findall(r"^\[(P\d+)\]$", text, _re.M)
            return json.dumps({
                "panels": [{"id": l, "story": True, "weight": 3 if l == "P1" else 2, "desc": f"event {l}", "chars": ["Kai"]} for l in labels],
                "summary": "Kai does things.", "title": "A Title",
                "characters": [{"name": "Kai", "aliases": [], "desc": "clerk"}], "terms": [],
            })
        if "merge character lists" in system:
            return '{"characters": [{"name": "Kai Ren", "aliases": ["Kai"], "desc": "clerk with a sword"}], "terms": []}'
        if "cold-open hook" in system:
            return '{"beats": [{"text": "Most people hate Mondays.", "panels": ["c0002:P1"], "emphasis": "hit"}]}'
        return '{"beats": [{"text": "Kai Ren grabs the sword.", "panels": ["P1", "P2"]}, {"text": "But the sword answers.", "panels": ["P3"]}]}'


def test_script_stage_parallel_end_to_end(samples, tmp_path, monkeypatch):
    import shutil as _sh

    from recaper import pipeline
    from recaper.llm import Chain

    src = tmp_path / "manga"
    _sh.copytree(samples / "manga", src)
    model = _ScriptedModel()
    monkeypatch.setattr(pipeline, "make_chain", lambda spec, role, need_vision: Chain([model], role))
    project = pipeline.Project(src, Config.load(src, {"jobs": 3}))
    project.stage_panels()
    script = project.stage_script()

    assert [c["cid"] for c in script["chapters"]] == ["c0001", "c0002", "c0003"]
    assert script["hook"][0]["panels"] == ["c0002-p0001"]
    assert script["chapters"][1]["beats"][0]["panels"] == ["c0002-p0001", "c0002-p0002"]
    bible = json.loads((src / ".recaper" / "bible.json").read_text("utf-8"))
    assert "Kai Ren" in bible["characters"]
    n_calls = len(model.calls)
    assert pipeline.Project(src, Config.load(src, {})).stage_panels() is None
    project2 = pipeline.Project(src, Config.load(src, {}))
    project2.stage_panels()
    assert project2.stage_script() == script and len(model.calls) == n_calls  # fully cached


def test_claude_plan_cap_message_fails_over_without_retries():
    capped = _Fake("claude", exc=RuntimeError("Claude Code returned an error result: You've hit your 5-hour limit · resets 3am"))
    backup = _Fake("gemini", reply='{"ok": 1}')
    assert Chain([capped, backup], "writer").complete_json("s", ["x"]) == {"ok": 1}
    assert capped.calls == 1
    only = Chain([_Fake("claude", exc=RuntimeError("You've hit your weekly limit"))], "writer")
    with pytest.raises(QuotaError, match="out of quota"):
        only.complete_json("s", ["x"])


# ---------------------------------------------------------------- web + downloader


def test_options_schema_covers_every_config_field_and_roundtrips(tmp_path):
    from recaper.web import options as opts

    sch = opts.schema()
    assert sch["unlabelled"] == []  # every Config field has a UI label/help
    keys = {o["key"] for o in sch["options"]}
    assert {"lang", "reader", "writer", "tts.engine", "tts.args", "render.style", "render.fps"} <= keys
    saved = opts.write_values(tmp_path, {"lang": "pt", "tts.speed": "1.3", "tts.args": '{"k": 1}', "render.fps": "60", "jobs": "6"})
    assert saved == {"lang": "pt", "tts.speed": 1.3, "tts.args": {"k": 1}, "render.fps": 60}  # jobs=6 is the default
    assert opts.read_values(tmp_path) == saved
    cfg = Config.load(tmp_path, {})
    assert cfg.lang == "pt" and cfg.tts.args == {"k": 1} and cfg.render.fps == 60
    with pytest.raises(ValueError):
        opts.write_values(tmp_path, {"render.height": "1081"})


def test_chapter_folder_names_sort_like_ingest():
    from recaper.download import chapter_folder_name
    from recaper.ingest import _chapter_sort_key
    from recaper.sources import ChapterInfo

    chs = [ChapterInfo("a", "10", None, "Ten: The <End>?", 3, "en"), ChapterInfo("b", "2.5", None, "", 3, "en"), ChapterInfo("c", "2", None, "", 3, "en")]
    names = [chapter_folder_name(c) for c in chs]
    assert names == ["Ch 0010 - Ten The End", "Ch 0002.5", "Ch 0002"]
    assert sorted(names, key=_chapter_sort_key) == ["Ch 0002", "Ch 0002.5", "Ch 0010 - Ten The End"]


def test_pick_chapters_dedupes_and_prefers_group():
    from recaper.cli import pick_chapters
    from recaper.sources import ChapterInfo

    chs = [
        ChapterInfo("x1", "1", None, "", 10, "en", group="Alpha"),
        ChapterInfo("y1", "1", None, "", 12, "en", group="Beta"),
        ChapterInfo("o2", "2", None, "", 0, "en", group="Official", external_url="https://example.com"),
        ChapterInfo("x2", "2", None, "", 9, "en", group="Alpha"),
        ChapterInfo("x3", "3", None, "", 9, "en", group="Alpha"),
    ]
    assert [c.id for c in pick_chapters(chs, None, None)] == ["y1", "x2", "x3"]  # most pages, skip external
    assert [c.id for c in pick_chapters(chs, "1-2", "alpha")] == ["x1", "x2"]


def test_web_api_projects_config_and_safety(tmp_path):
    from fastapi.testclient import TestClient

    from recaper.web.server import create_app

    c = TestClient(create_app(tmp_path))
    assert c.get("/").status_code == 200 and c.get("/app.js").status_code == 200
    assert c.post("/api/projects", json={"name": "My Series"}).status_code == 200
    assert c.post("/api/projects", json={"name": "../escape"}).status_code == 400
    r = c.post("/api/projects/My Series/upload", files=[("files", ("Chapter 1/001.png", b"\x89PNG fake", "image/png")),
                                                        ("files", ("evil.exe", b"MZ", "application/octet-stream"))])
    assert r.json()["saved"] == 1 and (tmp_path / "My Series" / "Chapter 1" / "001.png").exists()
    assert c.put("/api/projects/My Series/config", json={"lang": "es"}).json()["saved"] == {"lang": "es"}
    assert c.get("/api/projects/My Series").json()["values"] == {"lang": "es"}
    assert c.get("/api/projects/My Series/file/..%2F..%2Fx").status_code == 404
    assert c.get("/api/projects/My Series/script").status_code == 404
    assert c.post("/api/projects/My Series/run", json={"until": "bogus"}).status_code == 400


# ------------------------------------------------------- looks, options, sources


def test_backgrounds_and_looks_render(tmp_path):
    from recaper.render.styles import make_background, make_style

    assert make_background("black", 64, 36).mean() < 30
    assert make_background("white", 64, 36).mean() > 200
    assert make_background("#ff0000", 64, 36)[18, 32].tolist()[2] > 200  # BGR: red channel
    assert make_background("blur", 64, 36) is None
    with pytest.raises(ValueError):
        make_background("rainbow", 8, 8)
    infos = _panels(tmp_path)
    for look in ({"background": "black", "layout": "row", "reveal": "together", "card_border": False},
                 {"background": "blur", "camera": 0, "hit_effect": 0, "shadow": 0, "transition_speed": 2}):
        st = make_style("motion", 640, 360, infos, look)
        prev = Shot(0, 2, ["a"], "normal", 0)
        shot = Shot(2, 5, ["a", "b"], "hit", 1)
        for t in (0.0, 0.2, 2.9):
            assert st.frame(shot, prev, t).shape == (360, 640, 3)
    bf = make_style("blurfill", 640, 360, infos, {"bg_dim": 0.1, "bg_blur": 0, "fill": 0.6})
    assert bf.frame(Shot(0, 2, ["tall"], "reveal", 0), None, 1.0).shape == (360, 640, 3)


def test_bool_and_colour_options(tmp_path):
    from recaper.web import options as opts

    saved = opts.write_values(tmp_path, {"render.background": "#112233", "render.card_border": False, "hook": "false",
                                         "panels.gutter": "0.04", "outro": "Subscribe!"})
    assert saved == {"render.background": "#112233", "render.card_border": False, "hook": False, "panels.gutter": 0.04, "outro": "Subscribe!"}
    cfg = Config.load(tmp_path, {})
    assert cfg.render.card_border is False and cfg.hook is False and cfg.panels.gutter == 0.04
    with pytest.raises(ValueError):
        opts.write_values(tmp_path, {"render.background": "not-a-colour"})
    with pytest.raises(ValueError):
        opts.write_values(tmp_path, {"render.fill": "1.5"})


def test_panel_settings_change_recuts_chapter(samples, tmp_path):
    from recaper.config import PanelsConfig
    from recaper.panels import panels_stale

    ch = discover(samples / "manhwa", tmp_path)[0]
    first = extract_chapter(ch, "manhwa", False, tmp_path)
    assert not panels_stale(ch, "manhwa", False, tmp_path, PanelsConfig())
    coarse = PanelsConfig(gutter=0.5)  # gutters must be huge -> fewer cuts
    assert panels_stale(ch, "manhwa", False, tmp_path, coarse)
    again = extract_chapter(ch, "manhwa", False, tmp_path, coarse)
    assert len(again) < len(first) and not panels_stale(ch, "manhwa", False, tmp_path, coarse)


def test_hook_off_and_outro(samples, tmp_path, monkeypatch):
    import shutil as _sh

    from recaper import pipeline
    from recaper.llm import Chain

    src = tmp_path / "manga"
    _sh.copytree(samples / "manga", src)
    model = _ScriptedModel()
    monkeypatch.setattr(pipeline, "make_chain", lambda spec, role, need_vision: Chain([model], role))
    project = pipeline.Project(src, Config.load(src, {"hook": False, "outro": "See you next time!"}))
    project.stage_panels()
    script = project.stage_script()
    assert script["hook"] == []
    assert script["chapters"][-1]["beats"][-1]["text"] == "See you next time!"
    assert not any("cold-open" in c for c in model.calls)


class _FakeSource:
    id = "fake"

    def __init__(self):
        from recaper.sources import Page

        self.Page = Page
        self.calls = 0

    def pages(self, chapter_id, quality="data"):
        if chapter_id.startswith("file"):
            return [self.Page(url="u", filename="book.pdf", kind="file")]
        return [self.Page(url=f"u{i}", filename=f"{i:03d}.jpg") for i in range(1, 4)]

    def fetch_image(self, page):
        self.calls += 1
        return b"img"

    def fetch_to_file(self, page, dest, progress=None):
        dest.write_bytes(b"%PDF-1.4 fake")
        if progress:
            progress(10, 10)

    def fetch_cover(self, url):
        raise RuntimeError("no cover")


def test_downloader_images_files_registry_and_hint(tmp_path):
    from recaper.download import download, downloaded_ids, format_hint
    from recaper.sources import ChapterInfo, MangaInfo

    src = _FakeSource()
    manga = MangaInfo("m1", "Test", tags=["Long Strip"], original_language="ko")
    chs = [ChapterInfo("img1", "1", None, "Start", 3, "en"), ChapterInfo("file1", "2", None, "Vol", 0, "en", kind="file", size=10),
           ChapterInfo("ext1", "3", None, "", 0, "en", external_url="https://official.example")]
    seen = []
    res = download(src, manga, chs, tmp_path / "P", progress=lambda d, t, m: seen.append((d, t)))
    assert sorted(res["downloaded"]) == ["file1", "img1"] and res["skipped_external"] == ["ext1"]
    assert (tmp_path / "P" / "Ch 0001 - Start" / "003.jpg").exists() and (tmp_path / "P" / "Ch 0002 - Vol.pdf").exists()
    assert downloaded_ids(tmp_path / "P") == {"img1", "file1"} and seen[-1][0] == seen[-1][1] == 4
    assert 'source = "manhwa"' in (tmp_path / "P" / "recaper.toml").read_text("utf-8")
    calls = src.calls
    assert download(src, manga, chs, tmp_path / "P")["skipped_existing"] == ["img1", "file1"] and src.calls == calls
    assert format_hint(type("C", (), {"id": "peppercarrot"})(), manga) == {"source": "manga", "reading": "ltr"}


def test_connector_info_exposes_filters():
    from recaper.sources import list_sources

    by_id = {s["id"]: s for s in list_sources()}
    assert {"mangadex", "archive", "peppercarrot"} <= set(by_id)
    assert by_id["mangadex"]["supports_nsfw"] and ["popular", "Most popular"] in by_id["mangadex"]["sorts"]
    assert "free" not in [a for a, _ in by_id["mangadex"]["availability"]]  # no licence data -> no free filter
    assert "free" in [a for a, _ in by_id["archive"]["availability"]] and "free" in [a for a, _ in by_id["peppercarrot"]["availability"]]


# ------------------------------------------------------------------- books


def _make_pdf(path, pages):
    """pages: list of (text, image_color or None, full_page_image: bool)."""
    import io

    import pymupdf
    from PIL import Image, ImageDraw

    doc = pymupdf.open()
    for text, color, full in pages:
        pg = doc.new_page(width=600, height=860)
        if color:
            im = Image.new("RGB", (900, 1300), color)
            ImageDraw.Draw(im).ellipse((100, 100, 800, 1200), outline=(0, 0, 0), width=9)
            buf = io.BytesIO()
            im.save(buf, "JPEG")
            rect = pg.rect if full else pymupdf.Rect(100, 440, 500, 820)
            pg.insert_image(rect, stream=buf.getvalue())
        if text:
            pg.insert_textbox(pymupdf.Rect(40, 40, 560, 420 if not full else 820), text, fontsize=8)
    doc.save(path)


def test_book_pdf_scanned_vs_digital_illustrations(tmp_path):
    from recaper.books import extract, is_book_project

    words = " ".join(["word"] * 300)
    (tmp_path / "scan").mkdir()
    _make_pdf(tmp_path / "scan" / "LN Vol 1.pdf", [(f"Chapter 1\n{words}", (250, 250, 245), True), (words, (250, 250, 245), True),
                                                    ("", (90, 140, 200), True), (f"Chapter 2\n{words}", (250, 250, 245), True)])
    chs = extract(tmp_path / "scan", tmp_path / "scan" / ".recaper", min_words=60)
    assert is_book_project(tmp_path / "scan")
    assert [c.title for c in chs] == ["Chapter 1", "Chapter 2"]
    assert sum(len(c.images) for c in chs) == 1  # only the text-less full-page illustration, not the scans
    (tmp_path / "manga").mkdir()
    _make_pdf(tmp_path / "manga" / "Ch 1.pdf", [("", (200, 200, 200), True)] * 4)
    assert not is_book_project(tmp_path / "manga")  # image-only PDFs stay manga


def test_txt_book_ignores_recaper_outputs(tmp_path):
    from recaper.books import book_files, extract

    body = " ".join(["Kai walks to the station."] * 60)
    (tmp_path / "Novel.txt").write_text(f"Prologue\n{body}\n\nChapter 1: Start\n{body}\n\nChapter 2: Gate\n{body}\n", "utf-8")
    (tmp_path / "novel_recap.chapters.txt").write_text("00:00 Intro\n", "utf-8")
    assert [f.name for f in book_files(tmp_path)] == ["Novel.txt"]
    assert [c.title for c in extract(tmp_path, tmp_path / ".recaper", min_words=50)] == ["Prologue", "Chapter 1: Start", "Chapter 2: Gate"]


class _BookModel(Provider):
    """Fake model for the book flow: digest, illustration, bible, map, writer, hook + manga reader."""

    name = "fakebook"

    def __init__(self):
        super().__init__(None)
        self.calls = []

    def complete(self, system, parts):
        import re as _re

        self.calls.append(system[:30])
        text = "\n".join(p for p in parts if isinstance(p, str))
        if "ONE chapter of a light novel" in system:
            return json.dumps({"story": True, "title": "A chapter", "summary": "Kai fights a goblin at the office.",
                               "events": [{"id": "E1", "desc": "Kai fights", "chars": ["Kai"], "weight": 3}], "characters": [{"name": "Kai", "desc": "clerk"}]})
        if "illustrations from a light novel" in system:
            return '{"panels": []}'
        if "meticulous manga" in system:
            labels = _re.findall(r"^\[(P\d+)\]$", text, _re.M)
            return json.dumps({"panels": [{"id": l, "story": True, "weight": 2, "desc": f"panel {l}", "chars": ["Kai"]} for l in labels],
                               "summary": "Kai fights a goblin.", "title": "Manga ch", "characters": [], "terms": []})
        if "merge character lists" in system:
            return '{"characters": [{"name": "Kai", "aliases": [], "desc": "clerk"}], "terms": []}'
        if "match chapters of a book" in system:
            keys = _re.findall(r"^(.+:c\d{4}):", text.split("ADAPTATION CHAPTERS:")[1], _re.M)
            books = _re.findall(r"^(c\d{4}):", text.split("ADAPTATION CHAPTERS:")[0], _re.M)
            return json.dumps({"map": {b: keys[:1] for b in books}})
        if "light-novel / book recap video" in system:
            ids = _re.findall(r"^([a-z]+-p\d{4}):", text, _re.M)
            return json.dumps({"beats": [{"text": "Hook line.", "panels": ids[:1], "emphasis": "hit"}]})
        if "recap videos of light novels" in system:
            return json.dumps({"beats": [{"text": "Kai swings.", "panels": ["R1"]}, {"text": "A huge gate opens.", "panels": [], "generate": "a gate in the sky"},
                                         {"text": "Then silence.", "panels": []}]})
        raise AssertionError(f"unexpected prompt: {system[:60]}")


def test_book_flow_related_works_and_fallback(samples, tmp_path, monkeypatch):
    import shutil as _sh

    from recaper import pipeline
    from recaper.llm import Chain

    lib = tmp_path / "lib"
    _sh.copytree(samples / "manga", lib / "Blade (manga)")
    book = lib / "Blade (book)"
    book.mkdir()
    body = " ".join(["Kai fights the goblin in the office."] * 40)
    (book / "Blade.txt").write_text(f"Chapter 1: Train\n{body}\n\nChapter 2: Office\n{body}\n", "utf-8")
    model = _BookModel()
    monkeypatch.setattr(pipeline, "make_chain", lambda spec, role, need_vision: Chain([model], role))
    cfg = Config.load(book, {"image_sources": "Blade (manga)", "images.generator": "none", "book.min_words": 50})
    project = pipeline.Project(book, cfg)
    project.stage_panels()
    assert project.source == "book" and len(project.selected) == 2
    script = project.stage_script()
    beats = [b for c in script["chapters"] for b in c["beats"]]
    assert all(b["panels"] for b in beats)  # every beat got a picture (no generator -> reuse)
    used = {p.split("-")[0] for b in beats for p in b["panels"]}
    assert used == {"xref"}  # adaptation panels imported into this project's namespace
    assert (book / ".recaper" / "panels" / "xref" / "panels.json").exists()
    assert any(b.get("generate") for b in beats)  # the request is kept in the script for the UI
    assert script["hook"] and script["hook"][0]["panels"][0].startswith("xref-")
    assert any("match chapters" in c for c in [m.lower() for m in model.calls]) or any("You match" in c for c in model.calls)


# ------------------------------------------------------- global settings + deleting


def test_global_settings_are_inherited_and_overridable(tmp_path):
    from recaper.config import global_config_path
    from recaper.web import options as opts

    proj = tmp_path / "series"
    proj.mkdir()
    opts.write_global({"lang": "pt", "render.background": "black", "tts.speed": 1.1})
    cfg = Config.load(proj, {})
    assert (cfg.lang, cfg.render.background, cfg.tts.speed) == ("pt", "black", 1.1)
    with pytest.raises(ValueError):
        opts.write_global({"chapters": "1-3"})  # per-series only
    # project: equal-to-global values are dropped; going back to the *default* is an override and is kept
    saved = opts.write_values(proj, {"lang": "pt", "render.background": "paper", "tts.speed": 1.1})
    assert saved == {"render.background": "paper"}
    cfg = Config.load(proj, {})
    assert (cfg.lang, cfg.render.background) == ("pt", "paper")
    assert opts.inherited(proj)["lang"] == "pt" and opts.inherited_from(proj)["lang"] == "global"
    assert Config.load(proj, {"lang": "es"}).lang == "es"  # CLI flags still win
    global_config_path().write_text("bogus_key = 1\n", "utf-8")
    with pytest.raises(ValueError, match="bogus_key"):
        Config.load(proj, {})


def test_web_settings_and_delete(tmp_path):
    from fastapi.testclient import TestClient

    from recaper.web.server import create_app

    c = TestClient(create_app(tmp_path))
    assert c.put("/api/settings", json={"lang": "fr", "writer": ""}).json()["saved"] == {"lang": "fr"}
    assert c.get("/api/settings").json()["values"] == {"lang": "fr"}
    assert c.put("/api/settings", json={"image_sources": "x"}).status_code == 400
    for name in ("A", "B"):
        c.post("/api/projects", json={"name": name})
        c.post(f"/api/projects/{name}/upload", files=[("files", ("Chapter 1/001.png", b"x", "image/png")),
                                                     ("files", ("Chapter 2/001.png", b"x", "image/png"))])
    detail = c.get("/api/projects/A").json()
    assert detail["inherited"]["lang"] == "fr" and detail["inherited_from"] == {"lang": "global"}
    # chapters
    a = tmp_path / "A"
    (a / ".downloads.json").write_text(json.dumps({"md-1": "Chapter 1", "md-2": "Chapter 2"}), "utf-8")
    assert c.delete("/api/projects/A/chapters", params={"file": "../B"}).status_code == 404
    r = c.delete("/api/projects/A/chapters", params={"file": "Chapter 1"})
    assert r.status_code == 200 and r.json()["project"]["chapters"] == 1 and not (a / "Chapter 1").exists()
    assert json.loads((a / ".downloads.json").read_text("utf-8")) == {"md-2": "Chapter 2"}
    # videos / cache
    (a / "a_recap.mp4").write_bytes(b"v")
    (a / "a_recap.chapters.txt").write_text("0:00 x", "utf-8")
    (a / ".recaper" / "panels").mkdir(parents=True)
    assert c.get("/api/projects/A/usage").json()["sizes"]["videos"] == 1
    assert c.delete("/api/projects/A/videos/..%2FB").status_code >= 400 and (tmp_path / "B").exists()
    assert c.delete("/api/projects/A/videos/x.txt").status_code == 404
    assert c.delete("/api/projects/A/videos/a_recap.mp4").status_code == 200 and not (a / "a_recap.chapters.txt").exists()
    assert c.delete("/api/projects/A", params={"what": "cache"}).status_code == 200 and not (a / ".recaper").exists()
    # whole project, unlinking it where it was an image source
    c.put("/api/projects/B/config", json={"image_sources": "A"})
    assert c.get("/api/projects/A/usage").json()["linked_by"] == ["B"]
    assert c.delete("/api/projects/A", params={"what": "bogus"}).status_code == 400
    assert c.delete("/api/projects/A").status_code == 200 and not a.exists()
    assert "image_sources" not in c.get("/api/projects/B").json()["values"]
    assert c.delete("/api/projects/..").status_code >= 400 and c.delete("/api/projects/.recaper").status_code == 400
    assert (tmp_path / "B").exists() and tmp_path.exists()


def test_antigravity_and_models_multiselect(tmp_path):
    from fastapi.testclient import TestClient

    from recaper.llm import make_chain, make_provider
    from recaper.llm.antigravity import resolve_model
    from recaper.web import options as opts
    from recaper.web.server import create_app

    avail = ["gemini-3.8-flash-high", "gemini-3.8-flash-low", "gemini-4-argon-high", "gemini-4-argon-medium"]
    assert resolve_model("gemini-4-argon-medium", avail) == "gemini-4-argon-medium"
    assert resolve_model("gemini-4-argon", avail) == "gemini-4-argon-high"
    assert resolve_model("argon", avail) == "gemini-4-argon-high"
    assert resolve_model("flash", avail) == "gemini-3.8-flash-high"
    p = make_provider("antigravity:gemini-4-argon-high")
    assert p.name == "antigravity" and p.model == "gemini-4-argon-high" and p.vision
    chain = make_chain("agy:gemini-4-argon-high,claude:opus", "reader", need_vision=True)
    assert [x.label() for x in chain.providers] == ["antigravity:gemini-4-argon-high", "claude:opus"]

    sch = opts.schema()
    by_key = {o["key"]: o for o in sch["options"]}
    assert by_key["reader"]["kind"] == "models" and by_key["writer"]["kind"] == "models"
    assert any(g["id"] == "antigravity" for g in sch["model_groups"])
    assert opts.coerce("reader", ["antigravity:gemini-4-argon-high", " claude:opus ", "antigravity:gemini-4-argon-high"]) == "antigravity:gemini-4-argon-high,claude:opus"
    with pytest.raises(ValueError, match="can't see images"):
        opts.write_global({"reader": "deepseek-free"})

    c = TestClient(create_app(tmp_path))
    assert any(g["id"] == "antigravity" for g in c.get("/api/models").json()["groups"])
    r = c.put("/api/settings", json={"reader": ["antigravity:gemini-4-argon-high", "claude:sonnet"], "writer": "deepseek-free,antigravity:gemini-3.8-flash-high"})
    assert r.status_code == 200
    assert r.json()["saved"] == {"reader": "antigravity:gemini-4-argon-high,claude:sonnet", "writer": "deepseek-free,antigravity:gemini-3.8-flash-high"}


def test_refusal_skips_retries_keeps_provider_alive_and_splits_book_chapter():
    from recaper.bookflow import _digest_prose
    from recaper.llm import RefusalError, extract_json
    from recaper.llm.base import is_refusal_message

    msg = "The prompt could not be submitted. The prompt contains sensitive words that violate Google's Generative AI Prohibited Use policy."
    assert is_refusal_message(msg)
    assert extract_json('Here is the output:\n```json\n{"a": "line1\nline2", "b": [1, 2,],}\n```\nDone.') == {"a": "line1\nline2", "b": [1, 2]}

    class _SelectiveRefuser(Provider):
        name = "antigravity"

        def __init__(self):
            super().__init__("gemini-4-argon-medium")
            self.calls = 0

        def complete(self, system, parts):
            self.calls += 1
            text = "\n".join(p for p in parts if isinstance(p, str))
            if "FORBIDDEN_WORD" in text:
                raise RefusalError(msg)
            return '{"story": true, "title": "Ch", "summary": "Part summary.", "events": [{"desc": "Part event", "chars": ["Rudeus"], "weight": 3}], "characters": [], "terms": []}'

    refuser = _SelectiveRefuser()
    backup = _Fake("claude", reply='{"ok": "from_backup"}')
    chain = Chain([refuser, backup], "writer")
    # 1) Refused prompt fails over immediately (1 call, 0 retries) to backup
    assert chain.complete_json("s", ["FORBIDDEN_WORD"]) == {"ok": "from_backup"}
    assert refuser.calls == 1 and backup.calls == 1
    # 2) Unlike QuotaError, RefusalError does NOT mark the primary provider dead for the next chapter
    assert chain.complete_json("s", ["clean chapter"])["title"] == "Ch"
    assert refuser.calls == 2 and backup.calls == 1

    # 3) Single-provider chain on a book chapter with one forbidden passage splits into segments and recovers the rest
    single = Chain([_SelectiveRefuser()], "writer")
    words = ["safe"] * 600 + ["FORBIDDEN_WORD"] + ["safe"] * 600
    res = _digest_prose("c0078", "Turning Point", words, "", single)
    assert len(res["events"]) >= 2 and "Part summary." in res["summary"]


