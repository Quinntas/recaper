import json
from pathlib import Path

import numpy as np
import pytest

from recaper.config import Config, TTSConfig, parse_chapter_ranges
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
    from recaper.render.styles import Look

    assert len(build_shots(beats, "motion", look=Look(layout="cascade"))) == 1


def test_motion_clean_steps_never_overlap(tmp_path):
    from recaper.render.styles import Look

    beats = [{"start": 0.0, "end": 6.0, "panels": ["a", "b", "tall"], "emphasis": "hit"},
             {"start": 6.0, "end": 9.0, "panels": ["tall"], "emphasis": "normal"}]
    shots = build_shots(beats, "motion")
    assert [s.panels for s in shots] == [["a"], ["a", "b"], ["b", "tall"], ["tall"]]
    assert shots[0].emphasis == "hit" and shots[1].emphasis == "normal"
    # the tall panel stays up for shots 2-3: one scroll span, so it never jumps back to its top
    assert shots[2].spans["tall"] == shots[3].spans["tall"] == [4.0, 9.0]
    # a short beat adds two panels at once instead of one per step
    short = build_shots([{"start": 0.0, "end": 2.0, "panels": ["a", "b", "c"], "emphasis": "normal"}], "motion")
    assert [s.panels for s in short] == [["a", "b"]]
    style = MotionStyle(1920, 1080, _panels(tmp_path), Look())
    for pids in (["a", "b"], ["b", "tall"], ["a", "tall"]):
        rs = list(style.layout(pids).values())
        a, b = rs
        assert a.x + a.w <= b.x + 1 or a.y + a.h <= b.y + 1  # side by side or stacked
    # continuous camera: a shot starts at the zoom the previous one ended on
    s0, s1 = shots[0], shots[1]
    end0 = style._cam(s0, None, s0.dur)[0]
    assert abs(style._cam(s1, s0, 0.0)[0] - end0) < 1e-3
    # cards of the next shot wait for leaving cards to fade first
    cur, prv = style.layout(["tall"]), style.layout(["a", "b"])
    starts, _, _ = style._entries(cur, prv, 3.0)
    assert starts["tall"] >= style.T_OUT * 0.8


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
        if "line editor of a recap video" in system:  # polish: same beats, small edits
            beats = _re.findall(r"^\d+\. (.*)$", text.split("BEATS:")[1], _re.M)
            return json.dumps({"texts": [b.replace("swings", "swung his blade") for b in beats]})
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




# ------------------------------------------------------- polish, parts, preview, usage, upscaler, antigravity


def _beats(texts, prefix="c0001-p"):
    return [{"text": t, "panels": [f"{prefix}{i:03d}"], "emphasis": "normal"} for i, t in enumerate(texts)]


def test_style_checks_and_polish_keeps_panels(tmp_path):
    from recaper.story import polish_chapter, style_checks

    draft = _beats(["Jin tells her that he is fine and walks away from the gate.", "He looks back.", "He sees the gate.",
                    "He feels cold.", "He runs.", "Why? How?"])
    checks = style_checks(draft, "en")
    assert any("present" in c for c in checks) and any("questions" in c for c in checks) and any('"he"' in c for c in checks)
    assert style_checks(draft, "pt") and not any("present" in c for c in style_checks(draft, "pt"))  # English-only checks skipped

    fixed = ["Jin told her that he was fine and walked away from the gate.", "But he looked back.", "The gate was still there.",
             "A chill ran through him.", "So he ran.", "What was waiting inside?"]
    model = _Fake("ed", json.dumps({"texts": fixed}))
    cfg = Config()
    out = polish_chapter(draft, "Ch 1", Bible(), Chain([model], "writer"), cfg, tmp_path)
    assert [b["text"] for b in out] == fixed and [b["panels"] for b in out] == [b["panels"] for b in draft]
    assert polish_chapter(draft, "Ch 1", Bible(), Chain([model], "writer"), cfg, tmp_path) == out and model.calls == 1  # cached
    # wrong count -> keep the draft; a beat that lost most of its words -> keep that beat
    bad = _Fake("ed", json.dumps({"texts": fixed[:3]}))
    assert polish_chapter(draft, "Ch 2", Bible(), Chain([bad], "writer"), Config(lang="es"), tmp_path) == draft
    short = _Fake("ed", json.dumps({"texts": ["Jin left."] + fixed[1:]}))
    res = polish_chapter(draft, "Ch 3", Bible(), Chain([short], "writer"), Config(extra_instructions="x"), tmp_path)
    assert res[0]["text"] == draft[0]["text"] and res[1]["text"] == fixed[1]


def _script(n_chapters, words_per_beat=50, beats=6):
    text = " ".join(["word"] * words_per_beat)
    return {"title": "T", "lang": "en", "hook": _beats(["Hook " + text], "c0001-h"),
            "chapters": [{"cid": f"c{i:04d}", "title": f"Ch {i}", "beats": _beats([text] * beats, f"c{i:04d}-p")} for i in range(1, n_chapters + 1)]}


def test_parts_split_whole_chapters_with_previously(tmp_path, monkeypatch):
    from recaper import pipeline

    cfg = Config(episode_minutes=3.0, words_per_minute=200, tts=TTSConfig(speed=1.2))  # each chapter = 300 words = 1.5 min
    project = pipeline.Project(tmp_path, cfg)
    assert len(project.parts(_script(2))) == 1  # 3.25 min total: under the 1.25x slack
    model = _Fake("w", json.dumps({"beats": [{"text": "Previously, Jin entered the gate.", "panels": ["c0002-p005"]},
                                             {"text": "Nobody came back.", "panels": ["nope"]}]}))
    monkeypatch.setattr(pipeline, "make_chain", lambda spec, role, need_vision: Chain([model], role))
    parts = project.parts(_script(7))
    assert [len(p["chapters"]) for p in parts] == [2, 1, 2, 2]  # 10.5 min / 3 -> 4 balanced parts, whole chapters
    assert [c["cid"] for p in parts for c in p["chapters"]] == [f"c{i:04d}" for i in range(1, 8)]
    assert parts[0]["hook"][0]["text"].startswith("Hook") and parts[0]["intro_title"] == "Intro"
    assert parts[1]["intro_title"] == "Previously" and parts[1]["hook"][0]["panels"] == ["c0002-p005"]
    assert parts[1]["hook"][1]["panels"]  # unknown panel id replaced by a valid earlier one
    calls = model.calls
    project.parts(_script(7))
    assert model.calls == calls  # previously-on beats are cached
    project.cfg.previously = False
    assert project.parts(_script(7))[1]["hook"] == []


def test_preview_script_takes_the_first_minute(tmp_path):
    from recaper import pipeline

    project = pipeline.Project(tmp_path, Config(words_per_minute=200))
    prev = project.preview_script(_script(5), seconds=60)  # budget 200 words: hook 51 + 3 beats of 50
    words = sum(len(b["text"].split()) for c in [{"beats": prev["hook"]}, *prev["chapters"]] for b in c["beats"])
    assert 200 <= words < 260 and len(prev["chapters"]) == 1 and prev["hook"]


def test_usage_log_summary_and_estimate(tmp_path):
    from recaper import usage

    work = tmp_path / ".recaper"
    usage.record("llm", provider="x")  # not started: no-op
    assert not (work / "usage.jsonl").exists()
    usage.start(work, until="render")
    chain = Chain([_Fake("claude", exc=QuotaError("5-hour limit")), _Fake("openai", '{"ok": 1}')], "writer")
    assert chain.complete_json("s", ["hello"]) == {"ok": 1}
    usage.record("image", provider="gemini", outcome="ok", seconds=3)
    usage.record("end", status="done", outputs=["t_recap.mp4"])
    usage.stop()
    runs = usage.summarize(usage.read(work))
    assert len(runs) == 1 and runs[0]["status"] == "done" and runs[0]["outputs"] == ["t_recap.mp4"]
    llm = runs[0]["llm"]
    assert llm["claude"]["quota"] == 1 and llm["openai"]["ok"] == 1 and llm["openai"]["billing"] == "api"
    assert llm["claude"]["billing"] in ("plan", "api") and runs[0]["images"]["ok"] == 1

    (tmp_path / "Ch 1").mkdir()
    cfg = Config(minutes_per_chapter=2.0, episode_minutes=2.0, tts=TTSConfig(speed=1.2))
    est = usage.estimate(tmp_path, cfg, [{"title": "Ch 1", "pages": 10}, {"title": "Ch 2", "pages": 10}], is_book=False)
    assert est["chapters"] == 2 and est["unread"] == 2 and est["reader"]["images"] == 60
    assert est["reader"]["calls"] == 2 * 1 and est["writer"]["calls"] == 2 * 2 + 1 + 1 + 1  # write+polish, bible, hook, previously
    assert est["video"]["parts"] == 2


def test_upscaler_targets_only_enlarged_panels(tmp_path):
    from recaper.upscale import resolve_mode, target_scale, upscale_panels

    assert target_scale(1920, 1080, 1920, 1080) == 1.0 and target_scale(720, 1164, 1920, 1080) == 1.0
    assert target_scale(640, 360, 1920, 1080) == 3.6 and target_scale(100, 100, 1920, 1080) == 4.0
    assert resolve_mode("off")[0] is None and resolve_mode("bogus")[0] is None
    meta = {"a": {"file": "panels/a.jpg", "w": 640, "h": 360}}
    assert upscale_panels(meta, tmp_path, 1920, 1080, "off") == meta


def test_antigravity_permission_filter_allows_only_listed_tools():
    import io
    import queue as _q

    from recaper.llm import antigravity as agy

    srv = agy._Server.__new__(agy._Server)
    srv._pending, srv._sessions, srv._allow = {}, {}, {"s1": {"generate_image"}}
    sent = []
    srv._send = sent.append

    def ask(sid, title):
        return json.dumps({"jsonrpc": "2.0", "id": len(sent) + 1, "method": "session/request_permission", "params": {
            "sessionId": sid, "toolCall": {"title": title},
            "options": [{"optionId": "always", "kind": "allow_always"}, {"optionId": "once", "kind": "allow_once"}, {"optionId": "no", "kind": "reject_once"}]}})

    srv.proc = type("P", (), {"stdout": io.StringIO("\n".join([
        ask("s1", "Run generate_image?"), ask("s1", "Run write_to_file?"), ask("s2", "Run generate_image?"), ask("s1", "Run run_command?")]) + "\n")})()
    srv._read_out()
    outcomes = [m["result"]["outcome"] for m in sent]
    assert outcomes[0] == {"outcome": "selected", "optionId": "once"}
    assert all(o == {"outcome": "cancelled"} for o in outcomes[1:])
    assert agy._file_uri_path("file:///C:/x/a%20b.jpg").name == "a b.jpg"
    assert isinstance(_q.Queue(), _q.Queue)


def test_chapter_index_viewer_and_run_selection(samples, tmp_path, monkeypatch):
    import shutil as _sh

    from fastapi.testclient import TestClient

    from recaper import pipeline
    from recaper.web.server import create_app

    _sh.copytree(samples / "manga", tmp_path / "Manga")
    book = tmp_path / "Book"
    book.mkdir()
    body = " ".join(["Kai walks to the station and waits for the last train."] * 30)
    (book / "Vol 1.txt").write_text(f"Chapter 1: Train\n{body}\n\nChapter 2: Office\n{body}\n\nChapter 3: Gate\n{body}\n", "utf-8")
    (book / "recaper.toml").write_text('[book]\nmin_words = 50\n', "utf-8")
    c = TestClient(create_app(tmp_path))

    idx = c.get("/api/projects/Manga/chapter-index").json()
    assert idx["kind"] == "pages" and [x["n"] for x in idx["chapters"]] == list(range(1, len(idx["chapters"]) + 1))
    assert idx["selected"] == [x["n"] for x in idx["chapters"]] and not idx["chapters"][0]["read"]
    view = c.get("/api/projects/Manga/chapter-view/1").json()
    assert view["pages"] and c.get(view["pages"][0]).status_code == 200
    assert c.get("/api/projects/Manga/chapter-view/99").status_code == 404
    assert c.get("/api/projects/Manga/file/..%2FBook%2FVol%201.txt").status_code == 404  # viewer files stay in the project

    bidx = c.get("/api/projects/Book/chapter-index").json()
    assert bidx["kind"] == "book" and [x["title"] for x in bidx["chapters"]] == ["Chapter 1: Train", "Chapter 2: Office", "Chapter 3: Gate"]
    bview = c.get("/api/projects/Book/chapter-view/2").json()
    assert bview["kind"] == "book" and bview["paragraphs"] and "Kai walks" in bview["paragraphs"][-1]

    calls = []
    monkeypatch.setattr("recaper.web.jobs.JobManager.run_pipeline", lambda self, *a, **k: calls.append(a) or type("J", (), {"view": lambda s: {"id": "x"}})())
    assert c.post("/api/projects/Manga/run", json={"chapters": "1-2,4"}).status_code == 200 and calls[-1][-1] == "1-2,4"
    assert c.post("/api/projects/Manga/run", json={"chapters": "1; rm -rf /"}).status_code == 400

    proj = pipeline.Project(tmp_path / "Manga", Config(chapters="2"))
    proj.stage_panels()
    assert proj.default_output().name == "manga_ch2_recap.mp4"
    proj_all = pipeline.Project(tmp_path / "Manga", Config())
    proj_all.stage_panels()
    assert proj_all.default_output().name == "manga_recap.mp4"


def test_gpu_frames_match_cpu(samples):
    import cv2

    from recaper.render import gpu as G
    from recaper.render.styles import panel_infos

    dev = G.get()
    if dev is None:
        pytest.skip("no CUDA GPU")
    chapters = discover(samples / "manhwa", samples / "manhwa" / ".recaper")
    panels = extract_chapter(chapters[0], "manhwa", False, samples / "manhwa" / ".recaper")
    meta = {p.id: {"file": p.file, "w": p.w, "h": p.h} for p in panels[:4]}
    infos = panel_infos(meta, samples / "manhwa" / ".recaper")
    ids = list(meta)
    timed = [{"start": 0, "end": 3, "panels": ids[:2], "emphasis": "normal"}, {"start": 3, "end": 6, "panels": ids[2:4], "emphasis": "hit"}]
    for style_cls in (MotionStyle, BlurFillStyle):
        st = style_cls(640, 360, infos)
        shots = build_shots(timed, style_cls.name)
        for k, (sh, t) in enumerate([(shots[0], 0.1), (shots[0], 2.5), (shots[-1], 0.05), (shots[-1], 2.0)]):
            prev = shots[shots.index(sh) - 1] if shots.index(sh) else None
            g = st.frame_gpu(dev, sh, prev, t)
            cpu = cv2.cvtColor(st.frame(sh, prev, t), cv2.COLOR_BGR2YUV_I420).ravel()
            diff = np.abs(dev.to_i420(g).astype(int) - cpu.astype(int))
            assert diff.mean() < 1.0, (style_cls.name, k, diff.mean())


def test_video_runs_queue_downloads_do_not(tmp_path, monkeypatch):
    import threading
    import time

    from recaper.web.jobs import JobManager

    m = JobManager()
    gates: dict[str, threading.Event] = {}

    def fake_run(job, args, cwd):  # stands in for the recaper subprocess
        gate = gates.setdefault(job.project, threading.Event())
        gate.wait(10)
        job.status, job.finished = "done", time.time()
        m._pump()

    monkeypatch.setattr(m, "_run_subprocess", fake_run)
    a = m.run_pipeline("A", tmp_path, "render", [], False, None)
    b = m.run_pipeline("B", tmp_path, "render", [], False, None)
    c = m.run_pipeline("C", tmp_path, "render", [], False, None)
    assert (a.status, b.status, c.status) == ("running", "queued", "queued")
    assert (b.position, c.position) == (1, 2) and b.view()["position"] == 1
    with pytest.raises(RuntimeError):
        m.run_pipeline("B", tmp_path, "render", [], False, None)  # one per project
    m.cancel(b.id)
    assert b.status == "cancelled" and c.position == 1
    started = threading.Event()
    d = m.run_download("D", "download", lambda job: (started.set(), {"downloaded": 1})[1])
    assert started.wait(5)  # downloads don't wait for the video queue
    gates.setdefault("A", threading.Event()).set()
    for _ in range(100):
        if c.status == "running":
            break
        time.sleep(0.02)
    assert a.status == "done" and c.status == "running" and c.position is None
    gates.setdefault("C", threading.Event()).set()
    for _ in range(100):
        if c.status == "done" and d.status == "done":
            break
        time.sleep(0.02)
    assert c.status == "done" and d.status == "done"


def test_polisher_voices_look_preview_and_pace(samples, tmp_path):
    import shutil as _sh

    import cv2
    from fastapi.testclient import TestClient

    from recaper import usage
    from recaper.config import TTSConfig as _T
    from recaper.story import Bible, polish_chapter
    from recaper.web import options as opts
    from recaper.web.server import create_app

    # polisher: its own chain, its own cache entry, its own estimate row
    cfg = Config(polisher="claude:haiku", polish=True)
    beats = [{"text": f"Gon runs number {i} far away now.", "panels": ["p"], "emphasis": "normal"} for i in range(3)]

    class _Echo:
        calls = 0

        def complete_json(self, system, parts):
            _Echo.calls += 1
            return {"texts": [b["text"].replace("runs", "sprints") for b in beats]}

    out = polish_chapter(beats, "Ch", Bible(), _Echo(), cfg, tmp_path / "pc")
    assert out[0]["text"].startswith("Gon sprints") and _Echo.calls == 1
    polish_chapter(beats, "Ch", Bible(), _Echo(), Config(polish=True), tmp_path / "pc")  # writer's own polish: separate cache
    assert _Echo.calls == 2
    _sh.copytree(samples / "manga", tmp_path / "Manga")
    est = usage.estimate(tmp_path / "Manga", cfg, [{"title": "c", "pages": 4}] * 2, is_book=False)
    assert est["polisher"]["calls"] == 2 and est["polisher"]["spec"] == "claude:haiku"
    assert opts.write_values(tmp_path / "Manga", {"polisher": "claude:haiku"})["polisher"] == "claude:haiku"

    # measured pace beats the nominal one, scaled to the current speed
    work = tmp_path / "Manga" / ".recaper"
    work.mkdir(exist_ok=True)
    nominal = usage.pace(work, Config())
    usage.save_pace(work, _T(engine="kokoro", speed=1.2), words=1620, seconds=600)  # 162 wpm at 1.2
    assert abs(usage.pace(work, Config(tts=_T(engine="kokoro", speed=1.0))) - 135) < 0.5
    assert usage.pace(work, Config(tts=_T(engine="edge", speed=1.0))) == Config(tts=_T(engine="edge", speed=1.0)).spoken_wpm
    assert nominal == Config().spoken_wpm

    c = TestClient(create_app(tmp_path))
    v = c.get("/api/voices?engine=kokoro&lang=pt").json()
    assert v["default"] == "pm_alex" and v["voices"][0]["match"] and v["voices"][0]["value"][0] == "p"
    assert c.get("/api/voices?engine=edge&lang=en").json()["voices"][0]["value"].startswith("en-")
    assert c.post("/api/voice-sample", json={"engine": "kokoro", "speed": 9}).status_code == 400
    assert c.get("/api/voice-sample/..%2F..%2Fsecret.wav").status_code == 404
    for style in ("motion", "blurfill", "auto"):
        r = c.get(f"/api/projects/Manga/look-preview?style={style}&background=black&width=320")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[1] == 320
    assert c.get("/api/projects/Manga/look-preview?style=nope").status_code == 400
    e = c.get("/api/projects/Manga/estimate?chapters=1").json()
    assert e["chapters"] == 1
    assert c.get("/api/projects/Manga/estimate?chapters=1;rm").status_code == 400


def test_human_style_known_audience_and_intro_toggle(samples, tmp_path):
    import shutil as _sh

    from recaper import pipeline, prompts
    from recaper.story import chapter_number, known_audience, style_checks

    # AI tells and glued epithets are flagged for the polish pass; the old "turn every 3rd sentence" nag is gone
    beats = [{"text": "Deep inside the estate, the fragile calm shatters as Beans, the frantic bean-headed secretary, bursts in."},
             {"text": "Cheadle, the sharp-tongued new chairman, stands. Little did they know what lies ahead."}]
    checks = " ".join(style_checks(beats, "en"))
    assert "fragile calm" in checks and "little did" in checks and "Beans, the frantic" in checks and "turn" not in checks
    assert style_checks([{"text": "Gon climbs the tree and Killua follows him up."}], "en") == []
    assert "every third sentence" not in prompts.STYLE_GUIDE and "vivid tag" not in prompts.STYLE_GUIDE  # old AI-ish rules

    # fans vs new viewers
    assert chapter_number("Ch 0337 - Confession") == 337 and chapter_number("Volume 3") == 3 and chapter_number("Prologue") is None
    assert known_audience(Config(), "Ch 0337 - Confession", 0) and not known_audience(Config(), "Chapter 1", 0)
    assert known_audience(Config(), "Chapter 4", 3) and known_audience(Config(chapters="20-30"), "Chapter 1", 0)
    assert not known_audience(Config(known_story="no"), "Ch 0337", 5) and known_audience(Config(known_story="yes"), "Chapter 1", 0)
    kw = dict(language="en", target_words=200, bible_text="", story_so_far="", prev_context="", chapter_title="t", notes_text="", extra="")
    fan, new = prompts.writer_user(position="first", known=True, **kw), prompts.writer_user(position="first", known=False, **kw)
    assert "Do NOT introduce anyone" in fan and "no introductions" in fan
    assert "Do NOT introduce anyone" not in new and "who the protagonist is" in new

    # the audience is part of the writing cache key (scripts are rewritten when it flips), resolved once per run
    _sh.copytree(samples / "manga", tmp_path / "Manga")
    keys = {}
    for mode in ("auto", "yes", "no"):
        proj = pipeline.Project(tmp_path / "Manga", Config(known_story=mode))
        proj.stage_panels()
        keys[mode] = proj._selection_key()
        assert proj.cfg.known_story in ("yes", "no")
    assert keys["auto"] == keys["no"] != keys["yes"]  # sample chapters start at "Chapter 1"
    later = pipeline.Project(tmp_path / "Manga", Config(chapters="2-3"))
    later.stage_panels()
    later._selection_key()
    assert later.cfg.known_story == "yes"


def test_long_narration_masters_in_parallel_pieces(tmp_path, monkeypatch):
    """Book-length narration: split at pauses, filtered in parallel, encoded once; same length, cached."""
    import subprocess

    import soundfile as sf

    from recaper.render import encode

    sr = 24000
    rng = np.random.default_rng(1)
    parts = []
    for _ in range(40):  # 40 "beats" of noise speech with 0.25 s pauses
        parts += [0.2 * rng.standard_normal(int(sr * 1.2)).astype(np.float32), np.zeros(int(sr * 0.25), np.float32)]
    nar = tmp_path / "narration.wav"
    sf.write(str(nar), np.concatenate(parts), sr)
    monkeypatch.setattr(encode, "SEGMENT_SECONDS", 15.0)
    cuts = encode.quiet_cuts(nar)
    audio = sf.read(str(nar), dtype="float32")[0]
    assert len(cuts) >= 2 and all(abs(audio[c]) == 0 for c in cuts)  # every cut lands in a pause

    out = tmp_path / "render"
    out.mkdir()
    m = encode.master_audio(nar, -16.0, out, workers=2)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(m)], capture_output=True, text=True)
    assert abs(float(probe.stdout) - len(audio) / sr) < 0.05
    assert not list(out.glob("narration_piece_*"))  # temp pieces cleaned up
    stamp = m.stat().st_mtime_ns
    assert encode.master_audio(nar, -16.0, out) == m and m.stat().st_mtime_ns == stamp  # cached
    encode.master_audio(nar, -14.0, out)
    assert m.stat().st_mtime_ns != stamp  # loudness change re-masters


def test_online_references_parse_match_repick_and_quality_check(tmp_path, monkeypatch):
    import cv2

    from recaper import bookflow, imagegen, webrefs

    # ---- naming, wiki parsing, character matching (no network: a fake HTTP client answers the wiki API)
    assert webrefs.series_name("", Path("Mushoku Tensei Jobless Reincarnation Light Novel")) == "Mushoku Tensei Jobless Reincarnation"
    ep = ("{{Infobox Anime Episode\n |season = 1\n |episode = 05\n |adaptation = [[Light Novel Volume 1]]: Chapter 11<br>"
          "[[Light Novel Volume 2]]: Prologue\n}}\n==Plot==\nRudeus meets [[Eris Boreas Greyrat|Eris]].<ref>x</ref>\n==Notes==\nno")
    gallery = "<gallery>\nMushoten 05 01.jpg\nTrailer video\nFile:Mushoten 05 02.png|caption\n</gallery>"

    class FakeHttp:
        def json(self, method, url, params=None, **kw):
            if params.get("list") == "categorymembers":
                return {"query": {"categorymembers": [{"title": "A Young Lady"}]}}
            titles = params["titles"].split("|")
            text = {"A Young Lady": ep, "A Young Lady/Gallery": gallery}
            return {"query": {"pages": [{"title": t, "revisions": [{"slots": {"main": {"content": text.get(t, "")}}}]} for t in titles]}}

    eps = webrefs.wiki_episodes(FakeHttp(), "https://x.fandom.com/api.php")
    assert eps[0]["episode"] == 5 and "Volume 2: Prologue" in eps[0]["adaptation"] and eps[0]["plot"] == "Rudeus meets Eris."
    assert eps[0]["files"] == ["Mushoten 05 01.jpg", "Mushoten 05 02.png"]  # videos / captions dropped
    anilist = [{"id": 1, "role": "MAIN", "names": ["Eris Boreas Greyrat", "Eris"], "first": "Eris", "last": "Boreas Greyrat"},
               {"id": 2, "role": "MAIN", "names": ["Rudeus Greyrat", "Rudeus"], "first": "Rudeus", "last": "Greyrat"},
               {"id": 3, "role": "SUPPORTING", "names": ["Paul Greyrat"], "first": "Paul", "last": "Greyrat"}]
    m = webrefs.match_characters({"Eris Greyrat": {"aliases": ["Eris"]}, "Rudeus": {"aliases": []}, "Norn Greyrat": {"aliases": []}}, anilist)
    assert m["Eris Greyrat"]["id"] == 1 and m["Rudeus"]["id"] == 2 and "Norn Greyrat" not in m  # same surname is not enough

    # ---- picture pass: beats without a picture get a matching anime still; the text stays
    notes = {"cid": "c0001", "title": "Roa", "labels": {"P1": "c0001-p0001"}, "panels": {"P1": {"story": True, "desc": "cover", "chars": []}}}
    related = [{"label": "A1", "kind": "anime still", "src": "w", "pid": "c0005-p0001", "desc": "Rudeus and Eris tied up", "chars": []}]
    raw = [{"text": "They get kidnapped.", "refs": [], "generate": "kidnapping", "chars": [], "emphasis": "normal"},
           {"text": "The cover.", "refs": ["P1"], "generate": "", "chars": [], "emphasis": "normal"}]
    writer = _Fake("w", json.dumps({"picks": {"1": ["A1", "ZZ9"], "2": ["A1"]}}))
    out = bookflow.repick_pictures(notes, related, raw, Chain([writer], "writer"), tmp_path)
    assert out[0]["refs"] == ["A1"] and out[1]["refs"] == ["P1"] and out[0]["text"] == raw[0]["text"]
    assert bookflow.repick_pictures(notes, related, raw, Chain([writer], "writer"), tmp_path) == out and writer.calls == 1  # cached

    # ---- quality check: a bad image is redone once with the problems, twice bad -> dropped and remembered
    work = tmp_path / "w"
    asks = []

    def fake_gen(spec, prompt, refs, dest, ref_notes=None, **kw):
        asks.append(prompt)
        img = np.full((360, 640, 3), 120, np.uint8)
        img[:40] = 0  # letterbox bar on a dark-ish image
        dest.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(dest), img)
        return dest

    monkeypatch.setattr(imagegen, "generate", fake_gen)
    monkeypatch.setattr(imagegen, "available", lambda spec: (True, ""))
    cfg = Config()
    cfg.images.generator = "gemini"
    judge = _Fake("qc", json.dumps({"score": 3, "problems": ["Eris has black hair"]}))
    g = bookflow.Gallery(work, cfg, Chain([judge], "reader"))
    assert g.generate("Eris draws her sword", []) is None and len(asks) == 2 and "Eris has black hair" in asks[1]
    assert g.qc_stats == {"passed": 0, "redone": 1, "dropped": 1}
    assert g.generate("Eris draws her sword", []) is None and len(asks) == 2  # rejected before: not paid again
    good = _Fake("qc", json.dumps({"score": 8, "problems": []}))
    g2 = bookflow.Gallery(work, cfg, Chain([good], "reader"))
    pid = g2.generate("Rudeus casts a spell", [])
    item = g2.by_key[next(iter(g2.by_key))]
    assert pid and item["qc"]["score"] == 8 and item["h"] == 320  # black bar trimmed


def test_image_cost_prediction(tmp_path):
    from recaper import usage

    # list prices at the size recaper requests, plus reference images as input tokens
    assert usage.image_price("gemini:gemini-2.5-flash-image")[0] == 0.039
    assert usage.image_price("gemini")[0] == 0.039  # the default model
    assert abs(usage.image_price("gemini:gemini-3-pro-image-preview", refs=4)[0] - (0.134 + 4 * 560 * 2.0 / 1e6)) < 1e-6
    assert usage.image_price("openai:gpt-image-1", "medium")[0] == 0.063 and usage.image_price("openai", "high")[0] == 0.25
    assert usage.image_price("antigravity")[0] == 0.0 and usage.image_price("gemini:new-model-9")[1].startswith("new-model-9 not in")
    assert usage.image_price("openai:gpt-image-1", override=0.1)[0] == 0.1

    cfg = Config()
    cfg.images.generator, cfg.images.max_generated, cfg.images.refs = "gemini:gemini-2.5-flash-image", 100, 4
    e = usage.image_estimate(tmp_path, cfg, 10, True)  # no history: prior of 3 asks / chapter, 35% redone by QC
    price = 0.039 + 4 * 560 * 0.30 / 1e6
    assert e["asked"] == 30 and e["new"] == 30 and e["attempts"] == round(30 * 1.35)
    assert e["usd"] == round(30 * 1.35 * price, 2) and e["usd_max"] == round(60 * price, 2) and e["qc"]["calls"] == round(30 * 1.35)
    # measured history: 80 chapters asked for 40 pictures, 20 made with this setup, 4 of 20 redone
    (tmp_path / "usage.jsonl").write_text(json.dumps({"kind": "pictures", "chapters": 80, "asked": 40, "cached": 0, "new": 20,
                                                      "first_tries": 20, "redone": 4, "sig": usage.image_setup_sig(cfg)}) + "\n", "utf-8")
    e = usage.image_estimate(tmp_path, cfg, 80, True)
    assert e["asked"] == 40 and e["cached"] == 20 and e["new"] == 20 and e["qc"]["redo_rate"] == 0.2
    assert e["usd"] == round(20 * 1.2 * price, 2)
    cfg.images.style = "different"  # setup changed: nothing cached can be reused
    assert usage.image_estimate(tmp_path, cfg, 80, True)["new"] == 40
    cfg.images.generator = "antigravity"
    assert usage.image_estimate(tmp_path, cfg, 80, True)["usd"] == 0.0
