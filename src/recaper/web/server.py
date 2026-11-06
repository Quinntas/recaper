"""recaper web UI: FastAPI backend + static single-page app.

Library = a folder of projects; each project is a folder of chapters (exactly what `recaper run` takes).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..config import CONFIG_NAME, WORK_DIR_NAME, Config
from ..books import BOOK_EXTS, is_output_file
from ..ingest import ARCHIVE_EXTS, IMAGE_EXTS, PDF_EXTS, _chapter_sort_key, natural_key
from . import options as opts
from .jobs import JobManager

STATIC = Path(__file__).parent / "static"
NAME_OK = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]{1,100}$")


class RunBody(BaseModel):
    until: str = "render"
    force: list[str] = []
    preview: bool = False
    output: str | None = None
    quick: bool = False  # first ~60 s at 480p
    chapters: str | None = None  # e.g. "1-5,8" (1-based, pipeline order); None = the project's setting


class CreateBody(BaseModel):
    name: str


class DownloadBody(BaseModel):
    source: str = "mangadex"
    manga_id: str
    lang: str = "en"
    chapter_ids: list[str]
    project: str | None = None
    quality: str = "data"  # data | data-saver
    run_after: bool = False
    link_to: str | None = None  # project that should use this download as an image source (related work)
    run: RunBody = RunBody()


def create_app(library: Path) -> FastAPI:
    from ..env import load_env

    load_env()
    library = library.resolve()
    library.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="recaper", version=__version__)
    jobs = JobManager()
    chapter_cache: dict[tuple, tuple[float, Any]] = {}
    doctor_cache: dict[str, Any] = {}

    # ---------------------------------------------------------------- helpers
    def project_dir(name: str, must_exist: bool = True) -> Path:
        if not NAME_OK.match(name) or name.startswith(".") or name in ("..", "."):
            raise HTTPException(400, "invalid project name")
        p = (library / name).resolve()
        if p.parent != library:
            raise HTTPException(400, "invalid project name")
        if must_exist and not p.is_dir():
            raise HTTPException(404, f"project {name!r} not found")
        return p

    def scan_chapters(p: Path) -> list[dict]:
        """Cheap listing (no archive extraction)."""
        out = []
        for child in p.iterdir():
            if child.name.startswith(".") or child.name == WORK_DIR_NAME:
                continue
            if child.is_dir():
                n = sum(1 for f in child.iterdir() if f.suffix.lower() in IMAGE_EXTS)
                if n:
                    out.append({"title": child.name, "pages": n, "kind": "folder", "file": child.name})
            elif child.suffix.lower() in BOOK_EXTS and not is_output_file(child):
                out.append({"title": child.stem, "pages": None, "kind": child.suffix.lower().lstrip("."), "file": child.name})
            elif child.suffix.lower() in ARCHIVE_EXTS | PDF_EXTS:
                # pages are known once the pipeline has extracted the file
                from ..ingest import _slug

                extracted = p / WORK_DIR_NAME / "pages" / _slug(child.stem)
                n = sum(1 for f in extracted.iterdir() if f.suffix.lower() in IMAGE_EXTS) if extracted.is_dir() else None
                out.append({"title": child.stem, "pages": n, "kind": child.suffix.lower().lstrip("."), "file": child.name})
        loose = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
        if loose and not out:
            out.append({"title": p.name, "pages": len(loose), "kind": "images"})
        out.sort(key=lambda c: _chapter_sort_key(c["title"]))
        return out

    def videos(p: Path) -> list[dict]:
        vids = []
        for f in sorted(p.glob("*.mp4"), key=lambda f: -f.stat().st_mtime):
            chap = f.with_suffix(".chapters.txt")
            vids.append({
                "name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime,
                "chapters": chap.read_text("utf-8") if chap.exists() else "",
            })  # fmt: skip
        return vids

    def summary(p: Path) -> dict:
        chs = scan_chapters(p)
        work = p / WORK_DIR_NAME
        meta = json.loads((p / ".source.json").read_text("utf-8")) if (p / ".source.json").exists() else None
        job = jobs.active_for(p.name)
        return {
            "name": p.name,
            "chapters": len(chs),
            "pages": sum(c["pages"] or 0 for c in chs),
            "has_script": (work / "script.json").exists(),
            "videos": [v["name"] for v in videos(p)],
            "modified": max([p.stat().st_mtime] + [f.stat().st_mtime for f in p.glob("*.mp4")]),
            "source": {
                "id": meta["source"], "title": meta["manga"]["title"], "manga_id": meta["manga"]["id"],
                "license": meta["manga"].get("license"), "license_verified": meta["manga"].get("license_verified", False),
                "license_note": meta["manga"].get("license_note"), "attribution": meta["manga"].get("attribution"),
                "url": meta["manga"].get("url"),
            } if meta else None,
            "active_job": job.id if job else None,
        }

    def find_panel(p: Path, pid: str) -> Path:
        cid = pid.split("-")[0]
        index = p / WORK_DIR_NAME / "panels" / cid / "panels.json"
        if not re.match(r"^[a-z]+\d*$", cid) or not index.exists():  # c0001, xref, gen, card
            raise HTTPException(404, "panel not found")
        for item in json.loads(index.read_text("utf-8")):
            if item["id"] == pid:
                return p / WORK_DIR_NAME / item["file"]
        raise HTTPException(404, "panel not found")

    # ---------------------------------------------------------------- general
    @app.get("/api/state")
    def state():
        return {"version": __version__, "library": str(library)}

    @app.get("/api/options")
    def options_schema():
        return opts.schema() | {"project_only": sorted(opts.PROJECT_ONLY)}

    @app.get("/api/models")
    def list_ai_models(live: bool = False):
        groups = opts.model_catalog()
        if live:
            try:
                from ..llm.antigravity import list_models

                agy = list_models()
                if agy:
                    for g in groups:
                        if g["id"] == "antigravity":
                            seen = {f"antigravity:{m['id']}" for m in agy}
                            g["models"] = (
                                [{"value": f"antigravity:{m['id']}", "label": m["name"], "vision": True} for m in agy]
                                + [m for m in g["models"] if m["value"] not in seen]
                            )
            except Exception:  # noqa: BLE001 - fall back to static catalog
                pass
        return {"groups": groups}

    # ---------------------------------------------------------------- global settings
    @app.get("/api/settings")
    def get_settings():
        from ..config import global_config_path

        path = global_config_path()
        try:
            values = opts.read_global()
        except Exception as e:  # noqa: BLE001  (hand-edited file with a typo)
            raise HTTPException(400, f"{path}: {e}") from e
        return {"values": values, "path": str(path), "toml": path.read_text("utf-8") if path.exists() else ""}

    @app.put("/api/settings")
    def put_settings(values: dict[str, Any]):
        try:
            saved, path = opts.write_global(values)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            raise HTTPException(400, str(e)) from e
        return {"saved": saved, "path": str(path), "toml": path.read_text("utf-8")}

    @app.get("/api/doctor")
    def doctor(refresh: bool = False):
        if doctor_cache and not refresh and time.time() - doctor_cache["at"] < 120:
            return doctor_cache["data"]
        from ..llm import make_provider
        from ..render.encode import nvenc_available
        from ..tts import ENGINE_CLASSES

        providers = {}
        for spec in ("claude:sonnet", "antigravity", "gemini", "openai", "ollama", "deepseek-free"):
            from ..config import _expand_aliases

            prov = make_provider(_expand_aliases(spec))
            try:
                if spec == "ollama":
                    import httpx

                    httpx.get("http://localhost:11434/api/tags", timeout=1.5).raise_for_status()
                else:
                    prov.check()
                providers[spec] = {"ok": True, "vision": prov.vision, "detail": ""}
            except Exception as e:  # noqa: BLE001
                providers[spec] = {"ok": False, "vision": prov.vision, "detail": str(e)[:200]}
        import importlib.util

        engines = {}
        for name, cls in ENGINE_CLASSES.items():
            mod = {"kokoro": "kokoro", "edge": "edge_tts", "coqui": "TTS", "xtts": "TTS", "openai": "openai",
                   "elevenlabs": "elevenlabs", "azure": "azure.cognitiveservices.speech", "system": "pyttsx3",
                   "gtts": "gtts"}.get(name)  # fmt: skip
            try:
                engines[name] = bool(mod and importlib.util.find_spec(mod.split(".")[0]))
            except (ImportError, ValueError):
                engines[name] = False
        data = {
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "nvenc": nvenc_available(),
            "providers": providers,
            "tts_engines": engines,
        }
        doctor_cache.update(at=time.time(), data=data)
        return data

    # ---------------------------------------------------------------- projects
    @app.get("/api/projects")
    def list_projects():
        out = []
        for p in library.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                try:
                    out.append(summary(p))
                except OSError:
                    continue
        return sorted(out, key=lambda s: -s["modified"])

    @app.post("/api/projects")
    def create_project(body: CreateBody):
        p = project_dir(body.name.strip(), must_exist=False)
        p.mkdir(exist_ok=True)
        return summary(p)

    @app.get("/api/projects/{name}")
    def get_project(name: str):
        p = project_dir(name)
        work = p / WORK_DIR_NAME
        cfg = Config.load(p, {})
        previews = sorted((work / "preview").glob("*.jpg"), key=lambda f: natural_key(f.name)) if (work / "preview").exists() else []
        bible = json.loads((work / "bible.json").read_text("utf-8")) if (work / "bible.json").exists() else None
        manifest = json.loads((work / "manifest.json").read_text("utf-8")) if (work / "manifest.json").exists() else {}
        return summary(p) | {
            "chapter_list": scan_chapters(p),
            "values": opts.read_values(p),
            "inherited": opts.inherited(p),
            "inherited_from": opts.inherited_from(p),
            "effective": {"reader": cfg.reader, "writer": cfg.writer, "style": cfg.render.style},
            "videos_detail": videos(p),
            "previews": [f.name for f in previews],
            "bible": bible,
            "detected_source": manifest.get("source"),
            "jobs": [j for j in jobs.list() if j["project"] == name][:20],
        }

    @app.put("/api/projects/{name}/config")
    def put_config(name: str, values: dict[str, Any]):
        p = project_dir(name)
        try:
            saved = opts.write_values(p, values)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            raise HTTPException(400, str(e)) from e
        return {"saved": saved, "toml": (p / CONFIG_NAME).read_text("utf-8")}

    # ---------------------------------------------------------------- deleting
    def _rmtree(path: Path) -> None:
        def unlock(func, target, _exc):  # read-only files (extracted archives, git checkouts) on Windows
            os.chmod(target, stat.S_IWRITE)
            func(target)

        for attempt in range(5):  # a just-killed job can hold file handles for a moment
            try:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(path, onexc=unlock)
                else:  # pragma: no cover
                    shutil.rmtree(path, onerror=unlock)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.5 * (attempt + 1))

    def _stop_jobs(name: str) -> None:
        job = jobs.active_for(name)
        if not job:
            return
        jobs.cancel(job.id)
        for _ in range(40):
            if job.status not in ("queued", "running"):
                return
            time.sleep(0.25)

    def _linked_by(name: str) -> list[str]:
        out = []
        for other in library.iterdir():
            if other.is_dir() and not other.name.startswith(".") and other.name != name:
                try:
                    links = [x.strip() for x in str(opts.read_values(other).get("image_sources") or "").split(",")]
                except Exception:  # noqa: BLE001
                    continue
                if name in links:
                    out.append(other.name)
        return out

    @app.get("/api/projects/{name}/usage")
    def project_usage(name: str):
        """What a delete would remove (shown in the confirm dialog)."""
        p = project_dir(name)
        sizes = {"chapters": 0, "videos": 0, "cache": 0, "total": 0}
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            n = f.stat().st_size
            sizes["total"] += n
            rel = f.relative_to(p)
            if rel.parts[0] == WORK_DIR_NAME:
                sizes["cache"] += n
            elif f.suffix.lower() == ".mp4" and len(rel.parts) == 1:
                sizes["videos"] += n
            else:
                sizes["chapters"] += n
        return {"sizes": sizes, "linked_by": _linked_by(name), "active_job": bool(jobs.active_for(name))}

    @app.delete("/api/projects/{name}")
    def delete_project(name: str, what: str = "all"):
        """what = all (the whole folder) | cache (.recaper only: re-cut, re-read, re-voice next run) | videos."""
        if what not in ("all", "cache", "videos"):
            raise HTTPException(400, "what must be all, cache or videos")
        p = project_dir(name)
        _stop_jobs(name)
        try:
            if what == "all":
                for other in _linked_by(name):  # don't leave dangling image sources behind
                    link_image_source(library / other, name, remove=True)
                _rmtree(p)
            elif what == "cache":
                _rmtree(p / WORK_DIR_NAME)
            else:
                for f in p.glob("*.mp4"):
                    f.unlink()
                    for side in (f.with_suffix(".chapters.txt"), f.with_suffix(".credits.txt")):
                        side.unlink(missing_ok=True)
        except OSError as e:
            raise HTTPException(409, f"could not delete (is a file open in another program?): {e}") from e
        return {"ok": True, "deleted": what}

    @app.delete("/api/projects/{name}/chapters")
    def delete_chapter(name: str, file: str):
        p = project_dir(name)
        if jobs.active_for(name):
            raise HTTPException(409, "stop the running job first")
        target = (p / file).resolve()
        known = {c["file"] for c in scan_chapters(p) if c.get("file")}
        if target.parent != p or file not in known:
            raise HTTPException(404, "chapter not found")
        try:
            if target.is_dir():
                _rmtree(target)
            else:
                target.unlink()
                from ..ingest import _slug

                _rmtree(p / WORK_DIR_NAME / "pages" / _slug(target.stem))  # extracted pages of a PDF/CBZ
        except OSError as e:
            raise HTTPException(409, f"could not delete: {e}") from e
        from ..download import REGISTRY

        reg = p / REGISTRY  # forget it was downloaded, so it can be downloaded again
        if reg.exists():
            data = {k: v for k, v in json.loads(reg.read_text("utf-8")).items() if Path(v).parts[:1] != (file,)}
            reg.write_text(json.dumps(data, indent=1), "utf-8")
        return {"ok": True, "project": summary(p)}

    @app.delete("/api/projects/{name}/videos/{video}")
    def delete_video(name: str, video: str):
        p = project_dir(name)
        target = (p / video).resolve()
        if target.parent != p or target.suffix.lower() != ".mp4" or not target.is_file():
            raise HTTPException(404, "video not found")
        try:
            target.unlink()
        except OSError as e:
            raise HTTPException(409, f"could not delete (is it playing somewhere?): {e}") from e
        for side in (target.with_suffix(".chapters.txt"), target.with_suffix(".credits.txt")):
            side.unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/projects/{name}/upload")
    async def upload(name: str, files: list[UploadFile] = File(...)):
        p = project_dir(name)
        saved = 0
        for f in files:
            rel = Path(*[part for part in re.split(r"[\\/]+", f.filename or "") if part not in ("", ".", "..")])
            if not rel.parts:
                continue
            ext = rel.suffix.lower()
            if ext not in IMAGE_EXTS | ARCHIVE_EXTS | PDF_EXTS | BOOK_EXTS:
                continue
            # folder uploads keep "<chapter folder>/<page>"; drop the outer picked folder if nested deeper
            if ext in IMAGE_EXTS and len(rel.parts) > 2:
                rel = Path(*rel.parts[-2:])
            dest = (p / rel).resolve()
            if p not in dest.parents:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            saved += 1
        return {"saved": saved, "project": summary(p)}

    @app.post("/api/projects/{name}/run")
    def run(name: str, body: RunBody):
        p = project_dir(name)
        if body.until not in ("panels", "read", "script", "voice", "render"):
            raise HTTPException(400, "bad stage")
        if body.output and not re.match(r"^[\w .()-]{1,80}\.mp4$", body.output):
            raise HTTPException(400, "output must be a simple .mp4 file name")
        if body.chapters and not re.match(r"^\s*\d+(\s*-\s*\d*)?(\s*,\s*\d+(\s*-\s*\d*)?)*\s*$", body.chapters):
            raise HTTPException(400, "chapters must look like 1-5,8")
        try:
            job = jobs.run_pipeline(name, p, body.until, body.force, body.preview, body.output, body.quick, body.chapters)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return job.view()

    # ---------------------------------------------------------------- chapter index / viewer
    def chapter_index(p: Path, cfg: Config) -> dict:
        """Chapters in pipeline order (the numbers --chapters uses), with their processing status."""
        from ..books import extract, is_book_project
        from ..ingest import list_entries

        work = p / WORK_DIR_NAME
        is_book = cfg.source == "book" or (cfg.source == "auto" and is_book_project(p))
        items: list[dict] = []
        if is_book:
            bc = cfg.book
            for i, c in enumerate(extract(p, work, bc.split_words, bc.min_words, bc.min_illustration)):
                items.append({"n": i + 1, "title": c.title, "volume": c.volume, "words": c.words, "images": len(c.images)})
        else:
            for i, e in enumerate(list_entries(p)):
                pages = None
                if e.kind in ("folder", "images"):
                    pages = sum(1 for f in e.path.iterdir() if f.suffix.lower() in IMAGE_EXTS)
                items.append({"n": i + 1, "title": e.title, "kind": e.kind, "file": e.path.relative_to(p).as_posix() if e.path != p else "", "pages": pages})
        script_dir = work / "script"
        for it in items:
            cid = f"c{it['n']:04d}"
            it["cid"] = cid
            it["read"] = (work / "notes" / f"{cid}.json").exists()
            it["written"] = script_dir.exists() and any(script_dir.glob(f"{cid}_*.json"))
        from ..config import parse_chapter_ranges

        try:
            selected = [i + 1 for i in parse_chapter_ranges(cfg.chapters, len(items))] if items else []
        except ValueError:
            selected = [it["n"] for it in items]
        return {"kind": "book" if is_book else "pages", "chapters": items, "selection": cfg.chapters or "", "selected": selected}

    @app.get("/api/projects/{name}/chapter-index")
    def get_chapter_index(name: str):
        p = project_dir(name)
        return chapter_index(p, Config.load(p, {}))

    @app.get("/api/projects/{name}/chapter-view/{n}")
    def chapter_view(name: str, n: int):
        """Pages (manga / manhwa) or text + illustrations (books) of chapter n, for the in-browser viewer."""
        from ..books import extract, is_book_project
        from ..ingest import entry_pages, list_entries

        p = project_dir(name)
        cfg = Config.load(p, {})
        work = p / WORK_DIR_NAME
        base = f"/api/projects/{name}/file/"

        def url(f: Path) -> str:
            from urllib.parse import quote

            return base + quote(f.relative_to(p).as_posix())

        if cfg.source == "book" or (cfg.source == "auto" and is_book_project(p)):
            bc = cfg.book
            chs = extract(p, work, bc.split_words, bc.min_words, bc.min_illustration)
            if not 1 <= n <= len(chs):
                raise HTTPException(404, "chapter not found")
            c = chs[n - 1]
            paras = [x.strip() for x in re.split(r"\n\s*\n|\n", c.text) if x.strip()]
            return {"kind": "book", "n": n, "count": len(chs), "title": c.title, "volume": c.volume, "paragraphs": paras,
                    "images": [url(work / rel) for rel in c.images if (work / rel).exists()]}  # fmt: skip
        entries = list_entries(p)
        if not 1 <= n <= len(entries):
            raise HTTPException(404, "chapter not found")
        e = entries[n - 1]
        try:
            pages = entry_pages(e, work)
        except Exception as ex:  # noqa: BLE001 - broken archive / PDF
            raise HTTPException(422, f"could not open {e.path.name}: {ex}") from ex
        return {"kind": "pages", "n": n, "count": len(entries), "title": e.title, "pages": [url(f) for f in pages]}

    @app.get("/api/projects/{name}/usage-log")
    def usage_log(name: str):
        """Past runs (model calls, quota hits, images, voice, renders) and an estimate of the next full run."""
        from .. import usage
        from ..books import is_book_project

        p = project_dir(name)
        cfg = Config.load(p, {})
        runs = usage.summarize(usage.read(p / WORK_DIR_NAME))[:30]
        is_book = cfg.source == "book" or (cfg.source == "auto" and is_book_project(p))
        try:
            est = usage.estimate(p, cfg, scan_chapters(p), is_book)
        except Exception as e:  # noqa: BLE001 - an estimate must never break the page
            est = {"error": str(e)[:200]}
        return {"runs": runs, "estimate": est, "is_book": is_book}

    @app.get("/api/projects/{name}/script")
    def get_script(name: str):
        p = project_dir(name)
        path = p / WORK_DIR_NAME / "script.json"
        if not path.exists():
            raise HTTPException(404, "no script yet")
        script = json.loads(path.read_text("utf-8"))
        panels: dict[str, list[str]] = {}
        for idx in sorted((p / WORK_DIR_NAME / "panels").glob("*/panels.json")):
            panels[idx.parent.name] = [x["id"] for x in json.loads(idx.read_text("utf-8"))]
        return {"script": script, "panels": panels}

    @app.put("/api/projects/{name}/script")
    def put_script(name: str, script: dict[str, Any]):
        p = project_dir(name)
        path = p / WORK_DIR_NAME / "script.json"
        if not path.exists():
            raise HTTPException(404, "no script yet")
        old = json.loads(path.read_text("utf-8"))
        try:
            sections = [script["hook"], *[c["beats"] for c in script["chapters"]]]
            for beats in sections:
                for b in beats:
                    if not isinstance(b.get("text"), str) or not b["text"].strip():
                        raise ValueError("every beat needs text")
                    if not b.get("panels") or not all(isinstance(x, str) for x in b["panels"]):
                        raise ValueError("every beat needs at least one panel")
                    if b.get("emphasis", "normal") not in ("normal", "hit", "reveal"):
                        raise ValueError("emphasis must be normal, hit or reveal")
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"invalid script: {e}") from e
        for k in ("title", "lang", "selection"):  # keep identity fields so the run reuses this script
            script[k] = old.get(k, script.get(k))
        path.write_text(json.dumps(script, ensure_ascii=False, indent=1), "utf-8")
        return {"ok": True}

    @app.get("/api/projects/{name}/panel/{pid}")
    def panel_image(name: str, pid: str):
        return FileResponse(find_panel(project_dir(name), pid), headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/projects/{name}/cover")
    def cover(name: str):
        p = project_dir(name)
        if (p / ".cover.jpg").exists():
            return FileResponse(p / ".cover.jpg")
        thumb = p / WORK_DIR_NAME / "thumb.jpg"
        if not thumb.exists():
            chs = [c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")]
            if not chs and (p / WORK_DIR_NAME / "pages").is_dir():  # pdf / cbz projects: extracted pages
                chs = [c for c in (p / WORK_DIR_NAME / "pages").iterdir() if c.is_dir()]
            if not chs:  # books: their illustrations, else a generated picture
                chs = [c for c in (p / WORK_DIR_NAME / "book").glob("*") if c.is_dir()] + [p / WORK_DIR_NAME / "panels" / "gen"]
                chs = [c for c in chs if c.is_dir()]
            chs.sort(key=lambda c: _chapter_sort_key(c.name))
            first = next((f for c in chs for f in sorted(c.iterdir(), key=lambda f: natural_key(f.name)) if f.suffix.lower() in IMAGE_EXTS), None)
            if not first:
                raise HTTPException(404, "no cover")
            from PIL import Image

            thumb.parent.mkdir(exist_ok=True)
            with Image.open(first) as im:
                im = im.convert("RGB")
                im = im.crop((0, 0, im.width, min(im.height, int(im.width * 1.45))))
                im.thumbnail((360, 520))
                im.save(thumb, "JPEG", quality=85)
        return FileResponse(thumb)

    @app.get("/api/projects/{name}/file/{rel:path}")
    def project_file(name: str, rel: str):
        p = project_dir(name)
        target = (p / rel).resolve()
        if p not in target.parents or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    # ---------------------------------------------------------------- sources
    @app.get("/api/sources")
    def sources():
        from ..sources import list_sources

        return list_sources()

    def connector(sid: str):
        from ..sources import get_connector

        try:
            return get_connector(sid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/sources/{sid}/search")
    def search(sid: str, q: str = "", lang: str | None = None, offset: int = 0, limit: int = 24,
               sort: str = "relevance", availability: str = "any", nsfw: bool = True):  # fmt: skip
        from dataclasses import asdict

        try:
            con = connector(sid)
            sort = sort if sort in con.sorts else con.sorts[0]
            availability = availability if availability in con.availability else "any"
            items = con.search(q, lang or None, limit=limit, offset=offset, sort=sort, availability=availability,
                               nsfw=nsfw and con.supports_nsfw)  # fmt: skip
            return [asdict(m) | {"nsfw": m.nsfw} for m in items]
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"{sid}: {e}") from e

    def cached_chapters(sid: str, mid: str, lang: str):
        key = (sid, mid, lang)
        hit = chapter_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        con = connector(sid)
        data = (con.manga(mid), con.chapters(mid, lang))
        chapter_cache[key] = (time.time(), data)
        return data

    @app.get("/api/sources/{sid}/manga/{mid}")
    def manga_detail(sid: str, mid: str, lang: str = "en"):
        from dataclasses import asdict

        try:
            manga, chapters = cached_chapters(sid, mid, lang)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"{sid}: {e}") from e
        from ..download import safe_project_name

        existing = library / safe_project_name(manga.title)
        from ..download import downloaded_ids

        have = downloaded_ids(existing) if existing.is_dir() else set()
        return {
            "manga": asdict(manga),
            "chapters": [asdict(c) | {"downloadable": c.downloadable, "downloaded": c.id in have} for c in chapters],
            "suggested_project": safe_project_name(manga.title),
        }

    @app.get("/api/sources/{sid}/cover")
    def source_cover(sid: str, url: str):
        try:
            data, ctype = connector(sid).fetch_cover(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404, str(e)) from e
        return Response(data, media_type=ctype, headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/download")
    def start_download(body: DownloadBody):
        from ..download import download, safe_project_name

        manga, chapters = cached_chapters(body.source, body.manga_id, body.lang)
        wanted = set(body.chapter_ids)
        picked = [c for c in chapters if c.id in wanted]
        if not picked:
            raise HTTPException(400, "no chapters selected")
        name = (body.project or safe_project_name(manga.title)).strip()
        p = project_dir(name, must_exist=False)
        con = connector(body.source)

        def work(job) -> dict:
            def prog(done: int, total: int, msg: str) -> None:
                job.stages["download"] = {"done": done, "total": total, "msg": msg}
                job.current = "download"
                if msg.endswith(("done", "already downloaded")) or "FAILED" in msg:
                    job.log.append(msg)

            job.log.append(f"downloading {len(picked)} chapter(s) of {manga.title} [{body.lang}] into {p.name}")
            return download(con, manga, picked, p, quality=body.quality, progress=prog, cancel=job._cancel)

        then = None
        link_target = project_dir(body.link_to) if body.link_to else None
        if body.run_after or link_target:
            def then() -> None:  # noqa: E306
                if link_target:
                    link_image_source(link_target, name)
                if body.run_after:
                    jobs.run_pipeline(name, p, body.run.until, body.run.force, body.run.preview, body.run.output, body.run.quick)

        job = jobs.run_download(name, f"Download {len(picked)} ch. of {manga.title}", work, then)
        return job.view()

    # ---------------------------------------------------------------- related works / images
    def link_image_source(target: Path, source_name: str, remove: bool = False) -> list[str]:
        values = opts.read_values(target)
        names = [x.strip() for x in str(values.get("image_sources") or "").split(",") if x.strip()]
        if remove:
            names = [x for x in names if x != source_name]
        elif source_name not in names and source_name != target.name:
            names.append(source_name)
        values["image_sources"] = ", ".join(names)
        opts.write_values(target, values)
        return names

    def series_title(p: Path) -> str:
        meta = p / ".source.json"
        title = json.loads(meta.read_text("utf-8"))["manga"]["title"] if meta.exists() else (Config.load(p, {}).title or p.name)
        title = re.sub(r"\((book|ln|light novel|novel|manga|manhwa)\)", " ", title, flags=re.I)
        title = re.sub(r"\b(light novel|web novel|novel|ln|volume \d+|vol\.? ?\d+)\b", " ", title, flags=re.I)
        return re.sub(r"\s+", " ", title).strip(" -:")

    @app.get("/api/projects/{name}/images")
    def project_images(name: str):
        p = project_dir(name)
        from ..imagegen import available

        cfg = Config.load(p, {})
        linked = [x.strip() for x in cfg.image_sources.split(",") if x.strip()]
        gen_index = p / WORK_DIR_NAME / "panels" / "gen" / "panels.json"
        generated = json.loads(gen_index.read_text("utf-8")) if gen_index.exists() else []
        ok, why = available(cfg.images.generator)
        return {
            "series_title": series_title(p),
            "linked": [{"name": n, "exists": (library / n).is_dir(), "chapters": len(scan_chapters(library / n)) if (library / n).is_dir() else 0} for n in linked],
            "generator": {"spec": cfg.images.generator, "available": ok, "detail": why, "max": cfg.images.max_generated},
            "generated": [{"id": g["id"], "prompt": g.get("prompt", "")} for g in generated],
        }

    @app.get("/api/projects/{name}/related")
    def related(name: str, q: str | None = None, lang: str = "en"):
        from dataclasses import asdict

        from ..sources import _REGISTRY, get_connector

        p = project_dir(name)
        query = (q or series_title(p)).strip()
        words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
        lib = []
        for other in library.iterdir():
            if other.is_dir() and not other.name.startswith(".") and other != p:
                ow = set(re.findall(r"\w+", series_title(other).lower()))
                if words and len(words & ow) >= max(1, len(words) // 2):
                    lib.append({"name": other.name, "chapters": len(scan_chapters(other))})
        results: dict[str, Any] = {}
        for sid in _REGISTRY:
            if sid == "peppercarrot":
                continue
            try:
                con = get_connector(sid)
                langs = [c for c, _ in con.languages]
                items = con.search(query, lang if lang in langs else (langs[0] if langs else None), limit=12,
                                   sort="relevance", availability="downloadable" if "downloadable" in con.availability else "any",
                                   nsfw=con.supports_nsfw)  # fmt: skip
                results[sid] = [asdict(m) | {"nsfw": m.nsfw} for m in items]
            except Exception as e:  # noqa: BLE001
                results[sid] = {"error": str(e)[:200]}
        return {"query": query, "library": lib, "sources": results}

    @app.post("/api/projects/{name}/image-sources")
    def edit_image_sources(name: str, body: dict[str, Any]):
        p = project_dir(name)
        src = str(body.get("name") or "").strip()
        if not src:
            raise HTTPException(400, "name required")
        if not body.get("remove"):
            project_dir(src)  # must exist in the library
        return {"linked": link_image_source(p, src, remove=bool(body.get("remove")))}

    # ---------------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, since: int = 0):
        try:
            return jobs.get(job_id).view(since)
        except KeyError as e:
            raise HTTPException(404, "job not found") from e

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        try:
            jobs.cancel(job_id)
        except KeyError as e:
            raise HTTPException(404, "job not found") from e
        return {"ok": True}

    # ---------------------------------------------------------------- static
    @app.exception_handler(HTTPException)
    async def http_error(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def serve(library: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    app = create_app(library)
    url = f"http://{host}:{port}"
    if open_browser:
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"recaper UI on {url}  (library: {library.resolve()})")
    uvicorn.run(app, host=host, port=port, log_level="warning")
