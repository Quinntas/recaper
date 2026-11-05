from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer

from .config import Config
from .env import load_env
from .log import console

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Turn a folder of manga / manhwa chapters into a narrated recap video.")
load_env()  # GEMINI_API_KEY / OPENAI_API_KEY etc. from .env (never printed)

STAGES = ["panels", "read", "script", "voice", "render"]


@app.command()
def run(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, help="Folder with chapter folders / .cbz / .pdf / images"),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="Narration language: en, pt, es, fr, ..."),
    style: Optional[str] = typer.Option(None, "--style", "-s", help="auto | motion (ref #1 motion-comic) | blurfill (ref #3)"),
    source: Optional[str] = typer.Option(None, help="auto | manga | manhwa"),
    reading: Optional[str] = typer.Option(None, help="Manga panel order: auto | rtl | ltr"),
    reader: Optional[str] = typer.Option(None, help="Vision model(s) that read panels, e.g. 'claude:sonnet,gemini'"),
    writer: Optional[str] = typer.Option(None, help="Model(s) that write the script, e.g. 'deepseek-free,claude:sonnet'"),
    tts: Optional[str] = typer.Option(None, "--tts", help="RealtimeTTS engine: kokoro | edge | coqui | openai | elevenlabs | system ..."),
    voice: Optional[str] = typer.Option(None, help="Voice name for the TTS engine (default depends on engine + language)"),
    speed: Optional[float] = typer.Option(None, help="Speech speed multiplier"),
    minutes_per_chapter: Optional[float] = typer.Option(None, "--mpc", help="Narration minutes per chapter (default 2.5)"),
    target_minutes: Optional[float] = typer.Option(None, "--minutes", help="Total video length target (overrides --mpc)"),
    chapters: Optional[str] = typer.Option(None, "--chapters", "-c", help="Chapter selection, e.g. '1-20' or '1-5,8'"),
    title: Optional[str] = typer.Option(None, help="Video title (default: folder name)"),
    extra: Optional[str] = typer.Option(None, help="Extra directions for the writer (tone, nicknames, ...)"),
    fps: Optional[int] = typer.Option(None, help="Frames per second (default 30)"),
    resolution: Optional[str] = typer.Option(None, help="WIDTHxHEIGHT (default 1920x1080)"),
    encoder: Optional[str] = typer.Option(None, help="auto | nvenc | x264"),
    workers: Optional[int] = typer.Option(None, help="Parallel render workers (default: auto)"),
    jobs: Optional[int] = typer.Option(None, "--jobs", "-j", help="Parallel model calls for reading/writing (default 6)"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output .mp4 path"),
    until: str = typer.Option("render", help="Stop after stage: panels | read | script | voice | render"),
    force: list[str] = typer.Option([], "--force", "-f", help="Redo a stage (repeatable): panels | read | script | voice | render"),
    preview: bool = typer.Option(False, help="Write panel contact sheets to .recaper/preview/ (check the slicing)"),
):
    """Run the pipeline (resumable: finished stages are reused)."""
    from .pipeline import Project

    overrides: dict = {
        "lang": lang,
        "source": source,
        "reading": reading,
        "reader": reader,
        "writer": writer,
        "minutes_per_chapter": minutes_per_chapter,
        "target_minutes": target_minutes,
        "chapters": chapters,
        "title": title,
        "extra_instructions": extra,
        "jobs": jobs,
        "tts.engine": tts,
        "tts.voice": voice,
        "tts.speed": speed,
        "render.style": style,
        "render.fps": fps,
        "render.encoder": encoder,
        "render.workers": workers,
    }
    if resolution:
        w, h = resolution.lower().split("x")
        overrides["render.width"], overrides["render.height"] = int(w), int(h)
    if until not in STAGES:
        raise typer.BadParameter(f"--until must be one of {STAGES}")

    cfg = Config.load(folder, overrides)
    project = Project(folder, cfg)
    if force:
        project.force(force)

    project.stage_panels(preview=preview)
    if until == "panels":
        return
    script = project.stage_script(until_read=(until == "read"))
    if until in ("read", "script"):
        return
    timed, narration, markers = project.stage_voice(script)
    if until == "voice":
        console.print(f"narration written to {narration}")
        return
    project.stage_render(timed, narration, markers, out)


@app.command()
def doctor(
    reader: str = typer.Option("claude:sonnet", help="Reader chain to test"),
    writer: str = typer.Option("claude:sonnet", help="Writer chain to test"),
    live: bool = typer.Option(False, help="Send a tiny real request to each provider"),
    tts: str = typer.Option("kokoro", "--tts", help="TTS engine to check"),
):
    """Check ffmpeg / NVENC / providers / TTS engine."""
    from .config import _expand_aliases
    from .llm import make_provider
    from .render.encode import nvenc_available

    console.print(f"ffmpeg: {shutil.which('ffmpeg') or '[red]missing[/]'}   NVENC: {'yes' if nvenc_available() else 'no (x264 fallback)'}")
    seen = set()
    for role, spec in (("reader", reader), ("writer", writer)):
        for s in _expand_aliases(spec).split(","):
            if s in seen:
                continue
            seen.add(s)
            p = make_provider(s)
            try:
                p.check()
                status = "[green]ok[/]"
                if live:
                    r = p.complete("Reply with JSON only.", ['Return {"ok": true}'])
                    status += f"  live: {r.strip()[:60]!r}"
            except Exception as e:  # noqa: BLE001
                status = f"[red]{e}[/]"
            console.print(f"{role:7s} {p.label():45s} vision={'yes' if p.vision else 'no ':3s} {status}")
    import importlib

    from .tts import ENGINE_CLASSES

    try:
        getattr(importlib.import_module("RealtimeTTS"), ENGINE_CLASSES.get(tts, tts))
        console.print(f"tts     {tts}: [green]installed[/]")
    except Exception as e:  # noqa: BLE001
        console.print(f"tts     {tts}: [red]not installed[/] ({type(e).__name__}) -> uv sync --extra {tts}")


@app.command()
def script(folder: Path = typer.Argument(..., exists=True, file_okay=False)):
    """Print the current script (hook + chapters) as plain text."""
    path = folder / ".recaper" / "script.json"
    if not path.exists():
        raise typer.BadParameter("no script yet - run `recaper run FOLDER --until script` first")
    data = json.loads(path.read_text("utf-8"))
    console.print(f"[bold]{data['title']}[/]\n\n[bold]Intro[/]")
    for b in data.get("hook", []):
        console.print(f"  {b['text']}  [dim]{' '.join(p.split('-')[-1] for p in b['panels'])}[/]")
    for ch in data["chapters"]:
        console.print(f"\n[bold]{ch['title']}[/] [dim]({ch['cid']})[/]")
        for b in ch["beats"]:
            console.print(f"  {b['text']}  [dim]{' '.join(p.split('-')[-1] for p in b['panels'])}[/]")


@app.command()
def ui(
    library: Path = typer.Option(Path("library"), "--library", "-L", help="Folder holding your projects (one sub-folder per series)"),
    host: str = typer.Option("127.0.0.1", help="Bind address (keep 127.0.0.1 unless you know you need LAN access)"),
    port: int = typer.Option(8765, help="Port"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open the browser"),
):
    """Start the web UI: library, downloader, all options, live progress, script editor, video player."""
    from .web.server import serve

    serve(library, host=host, port=port, open_browser=not no_open)


@app.command()
def download(
    query: str = typer.Argument(..., help="Title to search, or a MangaDex URL / id"),
    chapters: Optional[str] = typer.Option(None, "--chapters", "-c", help="Chapter numbers, e.g. '1-20' or '1-5,8' (default: all)"),
    lang: str = typer.Option("en", "--lang", "-l", help="Translation language (en, pt-br, es-la, ...)"),
    source: str = typer.Option("mangadex", help="Source connector"),
    library: Path = typer.Option(Path("library"), "--library", "-L", help="Projects folder"),
    project: Optional[str] = typer.Option(None, help="Project folder name (default: series title)"),
    group: Optional[str] = typer.Option(None, help="Prefer this scanlation group when a chapter has several"),
    quality: str = typer.Option("data", help="data (original) | data-saver"),
    pick: int = typer.Option(1, help="Which search result to use (1 = best match)"),
    make_video: bool = typer.Option(False, "--video", help="Run the recap pipeline afterwards"),
):
    """Download chapters into a project folder (then optionally make the video)."""
    from .download import download as do_download
    from .download import safe_project_name
    from .sources import get_connector

    con = get_connector(source)
    results = con.search(query, lang)
    if not results:
        raise typer.BadParameter(f"nothing found for {query!r} in {lang}")
    manga = results[min(pick, len(results)) - 1]
    console.print(f"[bold]{manga.title}[/]  ({manga.url})")
    all_ch = con.chapters(manga.id, lang)
    picked = pick_chapters(all_ch, chapters, group)
    if not picked:
        nums = [c.number for c in all_ch if c.number and _is_num(c.number)]
        span = f"{min(nums, key=float)}-{max(nums, key=float)}" if nums else "none"
        raise typer.BadParameter(f"no chapters match {chapters!r} in {lang}; available chapter numbers: {span} ({len(all_ch)} releases)")
    external = [c for c in picked if not c.downloadable]
    picked = [c for c in picked if c.downloadable]
    if external and not picked:
        raise typer.BadParameter("those chapters are only available on official sites (external links) - not downloadable")
    console.print(f"{len(picked)} chapter(s) to download" + (f", {len(external)} official-only skipped" if external else ""))
    dest = library / (project or safe_project_name(manga.title))

    def prog(done: int, total: int, msg: str) -> None:
        if msg.endswith(("done", "already downloaded")) or "FAILED" in msg:
            console.print(f"  [{done}/{total} pages] {msg}")

    result = do_download(con, manga, picked, dest, quality=quality, progress=prog)
    console.print(f"[green]downloaded {len(result['downloaded'])}[/], existing {len(result['skipped_existing'])}, failed {len(result['failed'])} -> {dest}")
    if make_video:
        run(folder=dest, lang=None, style=None, source=None, reading=None, reader=None, writer=None, tts=None, voice=None,
            speed=None, minutes_per_chapter=None, target_minutes=None, chapters=None, title=None, extra=None, fps=None,
            resolution=None, encoder=None, workers=None, jobs=None, out=None, until="render", force=[], preview=False)


def pick_chapters(all_ch, spec: Optional[str], group: Optional[str]):
    """One release per chapter number (preferred group first, then the one with most pages)."""
    by_num: dict = {}
    for c in all_ch:
        key = c.number or c.id
        cur = by_num.get(key)
        score = (c.downloadable, bool(group and group.lower() in c.group.lower()), c.pages)
        if cur is None or score > cur[0]:
            by_num[key] = (score, c)
    unique = [v[1] for v in by_num.values()]
    if not spec:
        return unique
    wanted: set[float] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = float(a or 0), float(b or 1e9)
            wanted.update(f for c in unique if c.number and _is_num(c.number) and lo <= (f := float(c.number)) <= hi)
        elif part:
            wanted.add(float(part))
    return [c for c in unique if c.number and _is_num(c.number) and float(c.number) in wanted]


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    app()
