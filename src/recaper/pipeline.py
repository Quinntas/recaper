"""Stage orchestration with on-disk caching under <input>/.recaper/.

Stages: panels -> read -> script -> voice -> render. Every stage reuses what's already on disk,
so a crashed or interrupted run resumes, and editing script.json only re-voices / re-renders
the beats that changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import WORK_DIR_NAME, Config, parse_chapter_ranges
from .books import is_book_project
from .ingest import Chapter, detect_source, discover
from .llm import make_chain
from . import progress
from .log import console
from .panels import Panel, contact_sheet, extract_chapter, panels_stale
from .story import (
    Bible,
    chapter_word_targets,
    positions,
    previous_context,
    read_chapter,
    reconcile_bible,
    story_before,
    write_chapter,
    write_hook,
)
from .tts import Voice, load_audio, voice_all

STAGES = ["panels", "read", "script", "voice", "render"]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "recap"


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Project:
    def __init__(self, input_dir: Path, cfg: Config):
        self.input_dir = input_dir.resolve()
        self.cfg = cfg
        self.work = self.input_dir / WORK_DIR_NAME
        self.work.mkdir(exist_ok=True)
        self.chapters: list[Chapter] = []
        self.selected: list[Chapter] = []
        self.panels: dict[str, list[Panel]] = {}
        self.source = cfg.source
        self.book_chapters: list = []

    # ----------------------------------------------------------------- utils
    def force(self, stages: list[str]) -> None:
        targets = {
            "panels": ["panels", "read_tiles", "notes", "script", "script.json", "manifest.json"],
            "read": ["notes", "script", "script.json"],
            "script": ["script", "script.json"],
            "voice": ["audio"],
            "render": ["render"],
        }
        for st in stages:
            for name in targets.get(st, []):
                p = self.work / name
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()

    def _selection_key(self) -> str:
        sel = [c.cid for c in self.selected]
        from . import prompts
        from .story import known_audience

        # resolve "auto" once for this run: every writer call (chapters, hook, books) reads cfg.known_story
        first = self.selected[0] if self.selected else None
        known = known_audience(self.cfg, first.title if first else None, first.index if first else 0)
        self.cfg.known_story = "yes" if known else "no"
        blob = json.dumps([sel, self.cfg.lang, self.cfg.minutes_per_chapter, self.cfg.target_minutes, self.cfg.extra_instructions,
                           prompts.STYLE_VERSION, known])
        return hashlib.sha1(blob.encode()).hexdigest()[:10]

    # ---------------------------------------------------------------- panels
    def stage_panels(self, preview: bool = False) -> None:
        if self.source == "book" or (self.source == "auto" and is_book_project(self.input_dir)):
            from . import bookflow

            bookflow.book_panels(self)
            if preview:
                for ch in self.selected:
                    if self.panels.get(ch.cid):
                        contact_sheet(self.panels[ch.cid], self.work, self.work / "preview" / f"{ch.cid}.jpg")
            return
        self.chapters = discover(self.input_dir, self.work)
        if not self.chapters:
            raise SystemExit(f"No chapters found in {self.input_dir}. Put chapter folders / .cbz / .pdf / images there.")
        manifest_path = self.work / "manifest.json"
        old = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}

        if self.source == "auto":
            self.source = old.get("source") or detect_source(self.chapters)
        reading = self.cfg.reading
        rtl = (reading == "rtl") or (reading == "auto" and self.source == "manga")

        # invalidate caches of chapters whose identity changed (e.g. a chapter was inserted)
        old_titles = old.get("chapters", {})
        for ch in self.chapters:
            if ch.cid in old_titles and old_titles[ch.cid] != f"{ch.title}|{len(ch.pages)}":
                console.print(f"[yellow]{ch.cid} changed ({old_titles[ch.cid]} -> {ch.title}); re-processing it[/]")
                shutil.rmtree(self.work / "panels" / ch.cid, ignore_errors=True)
                for f in [self.work / "notes" / f"{ch.cid}.json", *(self.work / "script").glob(f"{ch.cid}_*.json")]:
                    f.unlink(missing_ok=True)
                (self.work / "script.json").unlink(missing_ok=True)
        manifest_path.write_text(
            json.dumps({"source": self.source, "chapters": {c.cid: f"{c.title}|{len(c.pages)}" for c in self.chapters}}, indent=1),
            "utf-8",
        )

        idx = parse_chapter_ranges(self.cfg.chapters, len(self.chapters))
        self.selected = [self.chapters[i] for i in idx]
        console.print(
            f"[bold]{len(self.chapters)} chapters[/] found, {len(self.selected)} selected - "
            f"source: [bold]{self.source}[/]{' (right-to-left)' if self.source == 'manga' and rtl else ''}"
        )
        # notes for chapter N depend on chapters before it, so we always read from the first selected chapter on
        for i, ch in enumerate(self.selected, start=1):
            if panels_stale(ch, self.source, rtl, self.work, self.cfg.panels):
                console.print(f"[yellow]{ch.cid}: panel settings changed - re-cutting it and re-reading its panels[/]")
                (self.work / "notes" / f"{ch.cid}.json").unlink(missing_ok=True)
                for f in (self.work / "script").glob(f"{ch.cid}_*.json"):
                    f.unlink()
                (self.work / "script.json").unlink(missing_ok=True)
            panels = extract_chapter(ch, self.source, rtl, self.work, self.cfg.panels)
            self.panels[ch.cid] = panels
            progress.emit("panels", i, len(self.selected), f"{ch.title}: {len(panels)} panels")
            console.print(f"  {ch.cid} [dim]{ch.title}[/]: {len(ch.pages)} pages -> {len(panels)} panels")
            if preview:
                contact_sheet(panels, self.work, self.work / "preview" / f"{ch.cid}.jpg")
        if preview:
            console.print(f"panel previews: {self.work / 'preview'}")

    # ----------------------------------------------------------------- story
    def stage_script(self, until_read: bool = False) -> dict:
        script_path = self.work / "script.json"
        key = self._selection_key()
        # hook/outro only affect assembly: they must not invalidate the (expensive) chapter scripts
        assembly = key + hashlib.sha1(
            json.dumps([self.cfg.hook, self.cfg.hook_words, self.cfg.outro, self.cfg.image_sources, asdict(self.cfg.images), self.cfg.polish] + ([self.cfg.polisher] if self.cfg.polisher else [])).encode()
        ).hexdigest()[:6]
        if script_path.exists():
            existing = json.loads(script_path.read_text("utf-8"))
            if existing.get("selection") == assembly:
                console.print(f"using existing script: {script_path}")
                return existing
            console.print("[yellow]chapter selection / length / language changed - reassembling script.json[/]")

        reader = make_chain(self.cfg.reader, "reader", need_vision=True)
        writer = make_chain(self.cfg.writer, "writer", need_vision=False)
        # the polish pass can use its own (stronger) model: local writers draft, a better model line-edits
        self.polisher = make_chain(self.cfg.polisher, "polisher", need_vision=False) if self.cfg.polisher else writer
        for chain in (reader, writer, *([self.polisher] if self.polisher is not writer else [])):
            for p in chain.providers:
                try:
                    p.check()
                except Exception as e:  # noqa: BLE001
                    console.print(f"[yellow]{chain.role}: {p.label()} looks unavailable: {e}[/]")

        if self.source == "book":
            from . import bookflow

            res = bookflow.book_script(self, reader, writer, key, until_read)
            if res is None:
                return {}
            hook, chapters_out = res["hook"], res["chapters"]
        else:
            res = self._manga_script(reader, writer, key, until_read)
            if res is None:
                return {}
            hook, chapters_out = res
        if self.cfg.polish and self.source == "book":  # manga/manhwa chapters are polished as they are written
            hook, chapters_out = self._polish(hook, chapters_out, self.polisher)
        if self.cfg.outro.strip() and chapters_out and chapters_out[-1]["beats"]:
            last = chapters_out[-1]["beats"][-1]
            chapters_out[-1]["beats"] = [*chapters_out[-1]["beats"], {"text": self.cfg.outro.strip(), "panels": last["panels"][-1:], "emphasis": "normal"}]

        script = {
            "title": self.cfg.title or self.input_dir.name,
            "lang": self.cfg.lang,
            "selection": assembly,
            "hook": hook,
            "chapters": chapters_out,
        }
        script_path.write_text(json.dumps(script, ensure_ascii=False, indent=1), "utf-8")
        words = sum(len(b["text"].split()) for c in [{"beats": hook}, *chapters_out] for b in c["beats"])
        console.print(
            f"script: {script_path}  ({words} words, ~{words / self._pace():.0f} min) - "
            "edit it freely, then re-run to voice/render the changes"
        )
        return script


    def _polish(self, hook: list[dict], chapters_out: list[dict], writer) -> tuple[list[dict], list[dict]]:
        """Editing pass: one cached call per chapter fixes tics, tense slips, missing turns and name drift."""
        from .story import polish_chapter

        bible = Bible()
        bpath = self.work / "bible.json"
        if bpath.exists():
            data = json.loads(bpath.read_text("utf-8"))
            bible.characters, bible.terms = data.get("characters", []), data.get("terms", {})
        t0 = time.time()
        units = [("Intro", hook)] + [(c["title"], c["beats"]) for c in chapters_out]
        console.print(f"[bold]polishing[/] {len(units)} sections with {writer.describe()}")
        progress.emit("script", 0, len(units), "polishing")
        out: list = [None] * len(units)
        with ThreadPoolExecutor(max(1, self.cfg.jobs)) as pool:
            futs = {pool.submit(polish_chapter, beats, title, bible, writer, self.cfg, self.work / "script"): i
                    for i, (title, beats) in enumerate(units)}  # fmt: skip
            for k, f in enumerate(as_completed(futs), start=1):
                out[futs[f]] = f.result()
                progress.emit("script", k, len(units), "polished")
        console.print(f"  polished in {time.time() - t0:.0f}s")
        return out[0], [{**c, "beats": b} for c, b in zip(chapters_out, out[1:])]

    def _manga_script(self, reader, writer, key: str, until_read: bool):
        """Manga / manhwa: read panels -> notes -> beats. Returns (hook, chapters) or None after reading."""
        t0 = time.time()
        jobs = max(1, self.cfg.jobs)
        console.print(f"[bold]reading[/] with {reader.describe()} ({jobs} parallel)")
        chapters = self.selected
        notes_by_cid: dict[str, dict] = {}
        # Chapter 1 first so the main cast is named; the rest in parallel with that bible as context.
        progress.emit("read", 0, len(chapters), f"reading {chapters[0].title}")
        first = read_chapter(chapters[0], self.panels[chapters[0].cid], "", reader, self.cfg, self.work)
        notes_by_cid[first["cid"]] = first
        progress.emit("read", 1, len(chapters), chapters[0].title)
        seed = Bible()
        seed.merge(first.get("characters"), first.get("terms"))
        with ThreadPoolExecutor(jobs) as pool:
            futs = {
                pool.submit(read_chapter, ch, self.panels[ch.cid], seed.text(), reader, self.cfg, self.work): ch.cid
                for ch in chapters[1:]
            }
            for fut in as_completed(futs):
                notes_by_cid[futs[fut]] = fut.result()
                progress.emit("read", len(notes_by_cid), len(chapters), futs[fut])
        all_notes = [notes_by_cid[ch.cid] for ch in chapters]
        bible = reconcile_bible(all_notes, writer, self.work)
        console.print(f"  read {len(all_notes)} chapters in {time.time() - t0:.0f}s; bible: {len(bible.characters)} characters")
        if until_read:
            return None

        t0 = time.time()
        console.print(f"[bold]writing[/] ({self.cfg.lang}) with {writer.describe()} ({jobs} parallel)")
        targets = chapter_word_targets(all_notes, self.cfg)
        pos = positions(len(all_notes))
        script_dir = self.work / "script"
        from .story import polish_chapter

        def polished(beats: list[dict], title: str) -> list[dict]:
            return polish_chapter(beats, title, bible, self.polisher, self.cfg, script_dir) if self.cfg.polish else beats

        def hook_job() -> list[dict]:
            return polished(write_hook(all_notes, bible, writer, self.cfg, script_dir / f"hook_{key}_{self.cfg.hook_words}.json"), "Intro")

        def chapter_job(i: int, notes: dict, words: int, position: str) -> list[dict]:
            beats = write_chapter(notes, position, story_before(all_notes, i), previous_context(all_notes, i), words, bible, writer,
                                  self.cfg, script_dir / f"{notes['cid']}_{position}_{words}_{self.cfg.lang}_{key}.json")  # fmt: skip
            return polished(beats, notes["title"])

        if self.cfg.polish:
            console.print("  each chapter is polished as soon as it is written")
        with ThreadPoolExecutor(jobs) as pool:
            hook_fut = pool.submit(hook_job) if self.cfg.hook else None
            beat_futs = [
                pool.submit(chapter_job, i, notes, words, position)
                for i, (notes, words, position) in enumerate(zip(all_notes, targets, pos))
            ]
            todo = [f for f in (hook_fut, *beat_futs) if f is not None]
            progress.emit("script", 0, len(todo), "writing")
            for k, f in enumerate(as_completed(todo), start=1):
                f.result()
                progress.emit("script", k, len(todo), "written")
            chapters_out = [
                {"cid": n["cid"], "title": n["title"], "beats": f.result()} for n, f in zip(all_notes, beat_futs)
            ]
            hook = hook_fut.result() if hook_fut else []
        console.print(f"  wrote {len(chapters_out)} chapters + hook in {time.time() - t0:.0f}s")
        return hook, chapters_out

    # ----------------------------------------------------------------- voice
    def stage_voice(self, script: dict, name: str = "narration.wav") -> tuple[list[dict], Path, list[tuple[float, str]]]:
        tcfg = self.cfg.tts
        voice = Voice(tcfg, self.cfg.lang, self.work / "audio")
        sections = [(script.get("intro_title") or "Intro", script.get("hook", []))] + [(c["title"], c["beats"]) for c in script["chapters"]]
        all_beats = [b for _, beats in sections for b in beats]
        console.print(f"[bold]voice[/]: {len(all_beats)} beats with {tcfg.engine} ({voice.voice or 'default voice'})")

        t0 = time.time()

        def voiced(i: int, n: int) -> None:
            progress.emit("voice", i, n, "voicing")
            if i % 25 == 0 or i == n:
                console.print(f"  voiced {i}/{n} new")

        progress.emit("voice", 0, len(all_beats), "loading voice")
        new = [0]

        def counted(i: int, n: int) -> None:
            new[0] = n
            voiced(i, n)

        paths = voice_all(tcfg, self.cfg.lang, self.work / "audio", [b["text"] for b in all_beats], counted)
        from . import usage

        usage.record("voice", engine=tcfg.engine, lines=new[0], total_lines=len(all_beats), seconds=round(time.time() - t0, 1),
                     chars=sum(len(b["text"]) for b in all_beats))  # fmt: skip
        flat = [(b, si) for si, (_, beats) in enumerate(sections) for b in beats]
        clips: list[tuple[Path, dict, int]] = [(p, b, si) for p, (b, si) in zip(paths, flat)]
        console.print(f"  voice ready in {time.time() - t0:.0f}s")

        sr = sf.info(str(clips[0][0])).samplerate if clips else 24000
        t = 0.3
        timed: list[dict] = []
        markers: list[tuple[float, str]] = []
        placements: list[tuple[int, Path]] = []
        last_section = -1

        def clip_frames(path: Path) -> int:  # what load_audio will return, without loading it
            info = sf.info(str(path))
            return info.frames if info.samplerate == sr else int(round(info.frames * sr / info.samplerate))

        for path, beat, si in clips:
            if si != last_section:
                if last_section != -1:
                    t += tcfg.chapter_gap
                if sections[si][1]:
                    markers.append((0.0 if not markers else t, sections[si][0]))
                last_section = si
            placements.append((int(t * sr), path))
            timed.append({"start": t, "end": 0.0, "panels": beat["panels"], "emphasis": beat.get("emphasis", "normal")})
            t += clip_frames(path) / sr + tcfg.beat_gap
        total = t + 0.8
        for i, b in enumerate(timed):
            b["end"] = timed[i + 1]["start"] if i + 1 < len(timed) else total
        if timed:
            timed[0]["start"] = 0.0

        # streamed to disk clip by clip: a whole book is ~20 h of audio (6+ GB as one array);
        # RF64 past ~4 GB, where a plain WAV header overflows
        total_n = int(total * sr)
        narration = self.work / name
        fmt = "RF64" if total_n * 2 > 3_900_000_000 else "WAV"
        with sf.SoundFile(str(narration), "w", sr, 1, subtype="PCM_16", format=fmt) as wav:
            pos = 0

            def silence(n: int) -> None:
                nonlocal pos
                while n > 0:
                    k = min(n, sr * 10)
                    wav.write(np.zeros(k, np.float32))
                    pos, n = pos + k, n - k

            for start, path in placements:
                silence(start - pos)
                audio = load_audio(path, sr)[max(0, pos - start) : max(0, total_n - start)]
                wav.write(audio)
                pos += len(audio)
            silence(total_n - pos)
        words = sum(len(b["text"].split()) for b in all_beats)
        if words >= 300:  # the real pace (voice + pauses) makes later length estimates honest
            from . import usage

            usage.save_pace(self.work, tcfg, words, total)
        console.print(f"narration: {total / 60:.1f} min")
        return timed, narration, markers

    # ---------------------------------------------------------------- render
    def stage_render(self, timed: list[dict], narration: Path, markers: list[tuple[float, str]], out: Path | None, rcfg=None) -> Path:
        from .render import render_video

        rcfg = rcfg or self.cfg.render
        style = self.cfg.render.style
        if style == "auto":
            style = "motion" if self.source == "manga" else "blurfill"
        panel_meta = {p.id: asdict(p) for plist in self.panels.values() for p in plist}
        missing = {pid for b in timed for pid in b["panels"] if pid not in panel_meta}
        if missing:
            # script may reference panels of chapters outside the current selection (e.g. the hook)
            for pj in (self.work / "panels").glob("*/panels.json"):
                for p in json.loads(pj.read_text("utf-8")):
                    panel_meta.setdefault(p["id"], p)
            still = {pid for pid in missing if pid not in panel_meta}
            if still:
                console.print(f"[yellow]dropping unknown panel ids from script: {sorted(still)[:5]}...[/]")
                for b in timed:
                    b["panels"] = [p for p in b["panels"] if p in panel_meta]
                timed = [b for b in timed if b["panels"]] or timed

        title = self.cfg.title or self.input_dir.name
        out = out or self.input_dir / f"{_slug(title)}_recap.mp4"
        total = timed[-1]["end"]

        # chapter markers: YouTube description text + embedded mp4 chapters
        yt = "\n".join(f"{_fmt_ts(ts)} {name}" for ts, name in markers)
        out.with_suffix(".chapters.txt").write_text(yt + "\n", "utf-8")
        meta = self.work / f"{out.stem}.ffmeta"
        lines = [";FFMETADATA1", f"title={title}"]
        for i, (ts, name) in enumerate(markers):
            end = markers[i + 1][0] if i + 1 < len(markers) else total
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(ts * 1000)}", f"END={int(end * 1000)}", f"title={name}"]
        meta.write_text("\n".join(lines) + "\n", "utf-8")

        render_video(timed, panel_meta, style, rcfg, narration, meta, out, self.work)
        console.print(f"[bold green]done:[/] {out}")
        progress.emit("done", 1, 1, "video ready", output=str(out))
        return out

    # ------------------------------------------------------------ parts / preview
    def default_output(self) -> Path:
        name = _slug(self.cfg.title or self.input_dir.name)
        if self.cfg.chapters and self.chapters and len(self.selected) < len(self.chapters):  # a subset gets its own file
            name += "_ch" + re.sub(r"[^0-9-]+", "_", self.cfg.chapters.replace(" ", "")).strip("_")
        return self.input_dir / f"{name}_recap.mp4"

    def produce(self, script: dict, out: Path | None = None, until: str = "render", quick: bool = False) -> list[Path]:
        """Voice (+ render) the script: one video, several parts (episode_minutes), or a quick 480p preview."""
        base = out or self.default_output()
        if until == "render":
            from .render import prewarm

            try:
                prewarm(self.cfg.render)  # render workers start up while the narration is voiced
            except Exception:  # noqa: BLE001 - only an optimisation
                pass
        if quick:
            part = self.preview_script(script)
            timed, narration, markers = self.stage_voice(part, name="narration_preview.wav")
            if len(timed) > 1 and timed[-1]["end"] > 75:  # the word estimate ran long: cut at ~60 s of real audio
                keep = max(1, sum(1 for b in timed if b["start"] < 60))
                part = self._first_beats(part, keep)
                timed, narration, markers = self.stage_voice(part, name="narration_preview.wav")  # audio is cached
            if until == "voice":
                return []
            rcfg = replace(self.cfg.render, width=854, height=480, fps=24, quality=30, upscaler="off", chunk_seconds=10.0)
            return [self.stage_render(timed, narration, markers, base.with_name(f"{base.stem}_preview{base.suffix}"), rcfg)]
        parts = self.parts(script)
        if len(parts) > 1:  # one voice batch for every part: the TTS workers load their model once, not per part
            texts = [b["text"] for p in parts for c in [{"beats": p.get("hook", [])}, *p["chapters"]] for b in c["beats"]]
            t0 = time.time()
            console.print(f"[bold]voice[/]: {len(texts)} beats for {len(parts)} parts with {self.cfg.tts.engine}")
            new = [0]

            def tick(i: int, n: int) -> None:
                new[0] = n
                progress.emit("voice", i, n, "voicing")

            voice_all(self.cfg.tts, self.cfg.lang, self.work / "audio", texts, tick)
            from . import usage

            usage.record("voice", engine=self.cfg.tts.engine, lines=new[0], total_lines=len(texts), seconds=round(time.time() - t0, 1),
                         chars=sum(len(t) for t in texts))  # fmt: skip
            console.print(f"  voice ready in {time.time() - t0:.0f}s")
        outs: list[Path] = []
        for p in parts:
            suffix = f"_part{p['part']:02d}" if len(parts) > 1 else ""
            if len(parts) > 1:
                console.print(f"[bold]part {p['part']}/{len(parts)}[/]: {p['chapters'][0]['title']} - {p['chapters'][-1]['title']}")
            timed, narration, markers = self.stage_voice(p, name=f"narration{suffix}.wav")
            if until == "voice":
                continue
            outs.append(self.stage_render(timed, narration, markers, base.with_name(f"{base.stem}{suffix}{base.suffix}")))
        return outs

    def _pace(self) -> float:
        from . import usage

        return usage.pace(self.work, self.cfg)

    def preview_script(self, script: dict, seconds: float = 60.0) -> dict:
        """The opening ~minute: the hook plus the first beats, enough to judge look, voice and pacing."""
        budget = seconds * self._pace() / 60
        used = sum(len(b["text"].split()) for b in script.get("hook", []))
        chapters = []
        for c in script["chapters"]:
            beats = []
            for b in c["beats"]:
                if used >= budget and (beats or chapters):
                    break
                beats.append(b)
                used += len(b["text"].split())
            if beats:
                chapters.append({**c, "beats": beats})
            if used >= budget:
                break
        return {**script, "chapters": chapters}

    @staticmethod
    def _first_beats(script: dict, n: int) -> dict:
        hook = script.get("hook", [])[:n]
        left = n - len(hook)
        chapters = []
        for c in script["chapters"]:
            if left <= 0:
                break
            chapters.append({**c, "beats": c["beats"][:left]})
            left -= len(chapters[-1]["beats"])
        return {**script, "hook": hook, "chapters": chapters}

    def parts(self, script: dict) -> list[dict]:
        """Split into parts of about episode_minutes (whole chapters only). Parts 2+ open with 'previously on'."""
        mins, wpm = self.cfg.episode_minutes, self._pace()
        chapters = script["chapters"]

        def minutes(beats: list[dict]) -> float:
            return sum(len(b["text"].split()) for b in beats) / wpm

        total = minutes(script.get("hook", [])) + sum(minutes(c["beats"]) for c in chapters)
        if mins <= 0 or len(chapters) < 2 or total <= mins * 1.25:
            return [script]
        # balanced split: pick the part count, then put each chapter in the part its midpoint falls into
        lengths = [minutes(c["beats"]) for c in chapters]
        body = sum(lengths)
        k = min(len(chapters), max(2, int(body / mins + 0.5)))
        groups: list[list[dict]] = [[] for _ in range(k)]
        acc = 0.0
        for c, m in zip(chapters, lengths):
            groups[min(k - 1, int((acc + m / 2) / (body / k)))].append(c)
            acc += m
        groups = [g for g in groups if g]
        writer = None
        out = []
        for i, g in enumerate(groups):
            intro, intro_title = script.get("hook", []), "Intro"
            if i > 0:
                intro_title = "Previously"
                if self.cfg.previously:
                    writer = writer or make_chain(self.cfg.writer, "writer", need_vision=False)
                    intro = self._previously([c for gg in groups[:i] for c in gg], i + 1, writer)
                else:
                    intro = []
            out.append({**script, "part": i + 1, "parts": len(groups), "hook": intro, "intro_title": intro_title, "chapters": g})
        return out

    def _previously(self, before: list[dict], part: int, writer, words: int = 75) -> list[dict]:
        from . import prompts
        from .story import _cap_words, _clean_text

        story = []
        budget = 5000  # words of earlier script shown to the model: recent chapters in full, older ones trimmed
        for k, c in enumerate(reversed(before)):
            text = " ".join(b["text"] for b in c["beats"])
            cap = 600 if k < 3 else 120
            w = text.split()
            story.append(f"{c['title']}: {' '.join(w[:cap])}{'…' if len(w) > cap else ''}")
            budget -= min(len(w), cap)
            if budget <= 0:
                break
        story.reverse()
        beats_all = [b for c in before for b in c["beats"] if b.get("panels")]
        strong = [b for b in beats_all if b.get("emphasis") in ("hit", "reveal")] or beats_all
        step = max(1, len(strong) // 40)
        cands = {b["panels"][0]: b["text"][:110] for b in strong[::step]}
        last = before[-1]["beats"][-3:]  # where things stood at the end of the last part
        cands.update({b["panels"][-1]: b["text"][:110] for b in last if b.get("panels")})
        key = hashlib.sha1(json.dumps([story, sorted(cands), self.cfg.lang, part, words]).encode()).hexdigest()[:12]
        path = self.work / "script" / f"previously_{key}.json"
        if path.exists():
            return json.loads(path.read_text("utf-8"))["beats"]
        user = (f"LANGUAGE: {self.cfg.lang}\n\nSTORY SO FAR (script of the previous parts):\n" + "\n\n".join(story)
                + "\n\nIMAGES (id: what it shows):\n" + "\n".join(f"{pid}: {t}" for pid, t in cands.items()))  # fmt: skip
        system = prompts.PREVIOUSLY_SYSTEM.replace("{PART}", str(part)).replace("{WORDS}", str(words))
        try:
            res = writer.complete_json(system, [user])
        except Exception as e:  # noqa: BLE001 - a part without a recap is still a fine video
            console.print(f"[yellow]'previously on' for part {part} failed ({e}); starting without it[/]")
            return []
        beats = []
        for b in res.get("beats", []) if isinstance(res, dict) else []:
            text = _clean_text(str(b.get("text") or ""))
            pids = [p for p in b.get("panels") or [] if p in cands][:2] or list(cands)[-1:]
            if text and pids:
                beats.append({"text": text, "panels": pids, "emphasis": b.get("emphasis") or "normal"})
        beats = _cap_words(beats, int(words * 1.3))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"beats": beats}, ensure_ascii=False, indent=1), "utf-8")
        return beats
