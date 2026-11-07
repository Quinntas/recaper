"""Online references for book mode: official character designs (AniList) and anime screenshots with episode
plots and light-novel adaptation notes (the series' Fandom wiki).

The screenshots become an image source like a linked adaptation project: they live in <work>/web/.recaper/
(one "chapter" per episode: panels.json + notes with the vision reader's description of every still), so the
existing chapter matching (related_library) and import (XrefStore) use them unchanged. Character portraits are
matched to story-bible names and sent to the image generator as identity references.

Everything is cached: re-runs only fetch what is missing. Requests are spaced politely with an honest
User-Agent.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import httpx
import numpy as np

from . import __version__, progress, prompts
from .llm import Chain, ImagePart
from .log import console
from .panels import Panel, imwrite

if TYPE_CHECKING:
    from .pipeline import Project
    from .story import Bible

UA = f"recaper/{__version__} (personal recap video tool)"
ANILIST = "https://graphql.anilist.co"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
_NOISE = re.compile(r"\b(light ?novel|web ?novel|ln|wn|novel|manga|manhwa|anime|vol(?:ume)?\.? ?\d*|complete|official|english|series)\b", re.I)


class _Http:
    """httpx client + spacing limiter shared by threads (wikis and AniList ask for gentle clients)."""

    def __init__(self, per_second: float = 4.0):
        self.client = httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def _wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(4):
            self._wait()
            try:
                r = self.client.request(method, url, **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(float(r.headers.get("retry-after") or 2 * (attempt + 1)))
                    last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    continue
                r.raise_for_status()
                return r
            except httpx.TransportError as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{url}: {last}")

    def json(self, method: str, url: str, **kw) -> Any:
        return self.request(method, url, **kw).json()


# ----------------------------------------------------------------- naming


def series_name(cfg_series: str, project_dir: Path) -> str:
    """'Mushoku Tensei Jobless Reincarnation Light Novel' -> 'Mushoku Tensei Jobless Reincarnation'."""
    if cfg_series.strip():
        return cfg_series.strip()
    name = re.sub(r"[_\-]+", " ", project_dir.name)
    name = _NOISE.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip() or project_dir.name


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ----------------------------------------------------------------- AniList


_MEDIA_Q = """query ($s: String) { Page(perPage: 15) { media(search: $s, type: ANIME, sort: SEARCH_MATCH) {
  id format popularity title { romaji english native } synonyms } } }"""
_CHARS_Q = """query ($id: Int, $p: Int) { Media(id: $id) { characters(page: $p, perPage: 25, sort: [ROLE, RELEVANCE, ID]) {
  pageInfo { hasNextPage } edges { role node { id name { full first last alternative } image { large } } } } } }"""


def anilist_franchise(http: _Http, name: str) -> tuple[list[dict], list[dict]]:
    """Anime entries of the series (every season) and their characters (deduplicated)."""
    res = http.json("POST", ANILIST, json={"query": _MEDIA_Q, "variables": {"s": name}})
    media = res.get("data", {}).get("Page", {}).get("media") or []
    key = " ".join(_norm(name).split()[:2])
    picked = [m for m in media if m.get("format") in ("TV", "TV_SHORT", "MOVIE", "ONA", "OVA", "SPECIAL")
              and any(key in _norm(t) for t in [*(m["title"] or {}).values(), *(m.get("synonyms") or [])] if t)]  # fmt: skip
    chars: dict[int, dict] = {}
    for m in picked[:6]:
        for page in range(1, 5):
            r = http.json("POST", ANILIST, json={"query": _CHARS_Q, "variables": {"id": m["id"], "p": page}})
            block = (r.get("data", {}).get("Media") or {}).get("characters") or {}
            for e in block.get("edges") or []:
                node = e["node"]
                if node["id"] not in chars and (node.get("image") or {}).get("large"):
                    n = node["name"]
                    chars[node["id"]] = {"id": node["id"], "role": e.get("role"), "image": node["image"]["large"],
                                         "names": [x for x in [n.get("full"), n.get("first"), *(n.get("alternative") or [])] if x],
                                         "first": n.get("first") or "", "last": n.get("last") or ""}  # fmt: skip
            if not (block.get("pageInfo") or {}).get("hasNextPage"):
                break
    return picked, list(chars.values())


def match_characters(bible_chars: dict[str, dict], anilist: list[dict]) -> dict[str, dict]:
    """bible name -> AniList character. Exact full-name/alias matches win; otherwise the first names must agree
    and every other word of the shorter name must appear in the longer one (Eris <-> Eris Boreas Greyrat)."""
    out: dict[str, dict] = {}
    for bname, info in bible_chars.items():
        names = [bname, *(info.get("aliases") or [])]
        best, best_score = None, 0
        for c in anilist:
            cand = [_norm(x) for x in c["names"]]
            score = 0
            for n in map(_norm, names):
                if not n:
                    continue
                if n in cand:
                    score = max(score, 3 if n == cand[0] else 2)
                    continue
                a, b = n.split(), cand[0].split()
                if a and b and a[0] == b[0] and (set(a) <= set(b) or set(b) <= set(a)):
                    score = max(score, 2 if len(a) > 1 and len(b) > 1 else 1)
            if score > best_score or (score == best_score and best and c.get("role") == "MAIN" and best.get("role") != "MAIN"):
                best, best_score = c, score
        if best and best_score >= 2 or (best and best_score == 1 and len(_norm(bname).split()) == 1):
            out[bname] = best
    return out


# -------------------------------------------------------------------- wiki


def find_wiki(http: _Http, name: str, override: str = "") -> str | None:
    """The series' Fandom wiki api.php: the override (subdomain or URL) or a guess from the title."""
    cands: list[str] = []
    if override.strip():
        o = override.strip().rstrip("/")
        if o.startswith("http"):
            base = re.sub(r"/(wiki/.*|api\.php)$", "", o)
            cands.append(base + "/api.php")
        else:
            cands.append(f"https://{o}.fandom.com/api.php")
    words = _norm(name).split()
    for k in (2, 1, 3, len(words)):
        if words[:k]:
            for sub in ("".join(words[:k]), "-".join(words[:k])):
                url = f"https://{sub}.fandom.com/api.php"
                if url not in cands:
                    cands.append(url)
    for api in cands:
        try:
            r = http.request("GET", api, params={"action": "query", "meta": "siteinfo", "siprop": "general|statistics", "format": "json"})
            info = r.json()["query"]
        except Exception:  # noqa: BLE001 - try the next guess
            continue
        site = _norm(info["general"].get("sitename", ""))
        if override.strip() or (words and words[0] in site):
            return api
    return None


def _wiki_text(raw: str) -> str:
    s = re.sub(r"<ref[^>]*?/>|<ref[^>]*>.*?</ref>", "", raw, flags=re.S)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", s)
    s = re.sub(r"<[^>]+>", " ", s).replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", s).strip()


def _section(text: str, names: tuple[str, ...]) -> str:
    for n in names:
        m = re.search(rf"^==\s*{n}\s*==\s*$(.*?)(?=^==[^=]|\Z)", text, re.M | re.S | re.I)
        if m:
            return m.group(1)
    return ""


def _infobox(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"^\s*\|\s*([\w ]+?)\s*=\s*(.*)$", text, re.M):
        out.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return out


def _contents(http: _Http, api: str, titles: list[str]) -> dict[str, str]:
    """title -> wikitext (redirects followed), 50 titles per request."""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i : i + 50]
        r = http.json("GET", api, params={"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                                          "titles": "|".join(chunk), "redirects": 1, "format": "json", "formatversion": 2})  # fmt: skip
        q = r.get("query", {})
        alias = {x["to"]: x["from"] for x in (q.get("normalized") or []) + (q.get("redirects") or [])}
        for p in q.get("pages") or []:
            revs = p.get("revisions") or []
            if not revs:
                continue
            text = revs[0].get("slots", {}).get("main", {}).get("content") or revs[0].get("content") or ""
            t = p["title"]
            out[t] = text
            while t in alias:  # map back to the title we asked for
                t = alias[t]
                out[t] = text
    return out


def wiki_episodes(http: _Http, api: str) -> list[dict]:
    """[{title, season, episode, adaptation, plot, files: [gallery file names]}] in airing order."""
    titles, cont = [], {}
    while True:
        r = http.json("GET", api, params={"action": "query", "list": "categorymembers", "cmtitle": "Category:Episodes",
                                          "cmnamespace": 0, "cmlimit": 500, "format": "json", **cont})  # fmt: skip
        titles += [m["title"] for m in r.get("query", {}).get("categorymembers", [])]
        if "continue" not in r:
            break
        cont = r["continue"]
    pages = _contents(http, api, titles)
    eps = []
    for t in titles:
        text = pages.get(t, "")
        box = _infobox(text)
        ep = re.search(r"\d+", box.get("episode", ""))
        if not ep:
            continue
        season = re.search(r"\d+", box.get("season", "")) or re.search(r"\d+", box.get("series", ""))
        plot = _wiki_text(_section(text, ("Plot", "Summary", "Synopsis")))
        eps.append({"title": t, "season": int(season.group()) if season else 1, "episode": int(ep.group()),
                    "adaptation": _wiki_text(box.get("adaptation") or box.get("chapters") or ""), "plot": plot[:2500], "files": []})  # fmt: skip
    eps.sort(key=lambda e: (e["season"], e["episode"]))
    galleries = _contents(http, api, [f"{e['title']}/Gallery" for e in eps])
    for e in eps:
        text = galleries.get(f"{e['title']}/Gallery") or pages.get(e["title"], "")
        files = []
        for block in re.findall(r"<gallery[^>]*>(.*?)</gallery>", text, re.S | re.I):
            for line in block.splitlines():
                f = line.split("|", 1)[0].strip()
                f = re.sub(r"^(File|Image):", "", f, flags=re.I).strip()
                if f.lower().endswith(IMAGE_EXT):
                    files.append(f)
        e["files"] = list(dict.fromkeys(files))
    return eps


def _image_urls(http: _Http, api: str, files: list[str], width: int = 1920) -> dict[str, str]:
    """file name -> download URL (scaled down to `width` when larger)."""
    out: dict[str, str] = {}
    for i in range(0, len(files), 50):
        chunk = files[i : i + 50]
        r = http.json("GET", api, params={"action": "query", "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": width,
                                          "titles": "|".join(f"File:{f}" for f in chunk), "format": "json", "formatversion": 2})  # fmt: skip
        q = r.get("query", {})
        back = {x["to"]: x["from"] for x in q.get("normalized") or []}
        for p in q.get("pages") or []:
            info = (p.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            if url and info.get("width", 0) >= 640:
                name = back.get(p["title"], p["title"]).split(":", 1)[-1]
                out[name] = url
                out[p["title"].split(":", 1)[-1]] = url
    return out


# ------------------------------------------------------------------- build


def web_dir(work: Path) -> Path:
    return work / "web"


def _download(http: _Http, url: str, dest: Path, max_w: int = 1920, referer: str | None = None) -> tuple[int, int] | None:
    if dest.exists():
        img = cv2.imread(str(dest))
        return (img.shape[1], img.shape[0]) if img is not None else None
    try:
        # Fandom's image CDN refuses requests without the wiki as referer (hotlink protection)
        data = http.request("GET", url, headers={"Referer": referer} if referer else None).content
    except Exception as e:  # noqa: BLE001 - one missing still is fine
        from .imagegen import warn_once

        warn_once(f"  some online images could not be downloaded: {str(e).split(' for url')[0][:120]}")
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    if img.shape[1] > max_w:
        s = max_w / img.shape[1]
        img = cv2.resize(img, (max_w, round(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    imwrite(dest, img, quality=92)
    return img.shape[1], img.shape[0]


def build(project: "Project", bible: "Bible", reader: Chain) -> Path | None:
    """Fetch + describe online references once. Returns the pseudo-project folder for related_library, or None."""
    cfg, work = project.cfg, project.work
    ic = cfg.images
    if str(ic.web).lower() in ("off", "no", "false", "0", "none"):
        return None
    root = web_dir(work)
    rwork = root / ".recaper"
    name = series_name(ic.series, project.input_dir)
    state_file = root / "source.json"
    state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else {}
    want = {"series": name, "wiki": ic.wiki, "max": ic.web_max}
    http = _Http()
    try:
        if state.get("want") != want or not state.get("fetched"):
            console.print(f"[bold]online references[/] for {name!r}")
            progress.emit("read", None, None, "online references: searching")
            state = {"want": want, "fetched": False}
            try:
                media, chars = anilist_franchise(http, name)
            except Exception as e:  # noqa: BLE001 - references are a bonus, never fatal
                console.print(f"[yellow]  AniList lookup failed: {e}[/]")
                media, chars = [], []
            state["anilist"] = [{"id": m["id"], "title": (m["title"] or {}).get("english") or (m["title"] or {}).get("romaji")} for m in media]
            state["characters"] = chars
            api = None
            try:
                api = find_wiki(http, name, ic.wiki)
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]  wiki lookup failed: {e}[/]")
            state["wiki"] = api
            eps = []
            if api:
                try:
                    eps = wiki_episodes(http, api)
                except Exception as e:  # noqa: BLE001
                    console.print(f"[yellow]  reading the wiki's episodes failed: {e}[/]")
            state["episodes"] = eps
            state["fetched"] = True
            root.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
            console.print(f"  AniList: {len(media)} anime entries, {len(chars)} characters; wiki: {api or 'not found'}; "
                          f"{len(eps)} episodes with {sum(len(e['files']) for e in eps)} screenshots")  # fmt: skip

        _portraits(http, root, state.get("characters") or [], bible)
        eps = state.get("episodes") or []
        if not eps:
            return None
        _episode_stills(http, state["wiki"], root, eps, ic.web_max)
    finally:
        http.client.close()
    _describe(rwork, eps, bible, reader, cfg.jobs)
    have = list((rwork / "notes").glob("c*.json")) if (rwork / "notes").exists() else []
    return root if have else None


def _portraits(http: _Http, root: Path, chars: list[dict], bible: "Bible") -> None:
    """Download portraits of the characters the story bible knows; writes chars.json {bible name: file}."""
    matched = match_characters(bible.characters, chars)
    out_file = root / "chars.json"
    out: dict[str, str] = {}
    for bname, c in matched.items():
        dest = root / "chars" / f"{c['id']}.jpg"
        if _download(http, c["image"], dest, max_w=1024):
            out[bname] = dest.relative_to(root).as_posix()
    root.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    if out:
        console.print(f"  character designs: {len(out)}/{len(bible.characters)} story characters matched")


def _episode_stills(http: _Http, api: str, root: Path, eps: list[dict], cap: int) -> None:
    rwork = root / ".recaper"
    budget = cap
    todo: list[tuple[int, dict, list[str]]] = []
    for i, e in enumerate(eps, start=1):
        files = e["files"][: max(0, budget)]
        budget -= len(files)
        todo.append((i, e, files))
    names = [f for _, _, files in todo for f in files]
    urls_file = root / "urls.json"
    urls = json.loads(urls_file.read_text("utf-8")) if urls_file.exists() else {}
    missing = [f for f in names if f not in urls]
    if missing:
        urls.update(_image_urls(http, api, missing))
        urls_file.write_text(json.dumps(urls, ensure_ascii=False, indent=1), "utf-8")
    total = len(names)
    done = [0]
    lock = threading.Lock()

    def one(i: int, k: int, f: str) -> tuple[int, int, Panel | None]:
        cid = f"c{i:04d}"
        rel = Path("panels") / cid / f"p{k:04d}.jpg"
        size = _download(http, urls[f], rwork / rel, referer=api.rsplit("/", 1)[0] + "/") if f in urls else None
        with lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == total:
                progress.emit("read", None, None, f"online references: {done[0]}/{total} screenshots")
        return i, k, Panel(f"{cid}-p{k:04d}", cid, rel.as_posix(), size[0], size[1], None) if size else None

    jobs = [(i, k, f) for i, _, files in todo for k, f in enumerate(files, start=1)]
    got: dict[int, list[tuple[int, Panel]]] = {}
    with ThreadPoolExecutor(4) as pool:
        for i, k, p in pool.map(lambda j: one(*j), jobs):
            if p:
                got.setdefault(i, []).append((k, p))
    for i, e, files in todo:
        cid = f"c{i:04d}"
        panels = [p for _, p in sorted(got.get(i, []), key=lambda kp: kp[0])]
        d = rwork / "panels" / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "panels.json").write_text(json.dumps([asdict(p) for p in panels], indent=1), "utf-8")
        (d / "episode.json").write_text(json.dumps({k: e[k] for k in ("title", "season", "episode", "adaptation", "plot")}, ensure_ascii=False, indent=1), "utf-8")


def _describe(rwork: Path, eps: list[dict], bible: "Bible", reader: Chain, jobs: int) -> None:
    """The vision reader describes each episode's stills (who, what, where) - the writer picks by these."""
    from .story import _norm_label

    work_list = []
    for i, _ in enumerate(eps, start=1):
        cid = f"c{i:04d}"
        d = rwork / "panels" / cid
        note = rwork / "notes" / f"{cid}.json"
        if not (d / "panels.json").exists():
            continue
        panels = [Panel(**p) for p in json.loads((d / "panels.json").read_text("utf-8"))]
        if not panels:
            continue
        sig = hashlib.sha1(json.dumps([p.id for p in panels]).encode()).hexdigest()[:10]
        if note.exists() and json.loads(note.read_text("utf-8")).get("sig") == sig:
            continue
        work_list.append((cid, panels, json.loads((d / "episode.json").read_text("utf-8")), note, sig))
    if not work_list:
        return
    console.print(f"  describing {sum(len(w[1]) for w in work_list)} screenshots from {len(work_list)} episodes ({reader.describe()})")
    bible_text = bible.text()
    done = [0]

    def one(item) -> None:
        cid, panels, ep, note, sig = item
        labels = {f"P{k}": p for k, p in enumerate(panels, start=1)}
        head = (f"STORY BIBLE:\n{bible_text}\n\nThese are screenshots from the ANIME adaptation, season {ep['season']} episode "
                f"{ep['episode']} \"{ep['title']}\" (adapts: {ep['adaptation'] or 'unknown'}).\nEPISODE PLOT: {ep['plot'][:1800]}\n"
                "Name the characters (bible names) by their look and the plot. Mark title cards, logos, credits and "
                "blurry/transition frames as story=false.")  # fmt: skip
        parts: list = [head]
        for lab, p in labels.items():
            parts += [f"[{lab}]", ImagePart(rwork / p.file)]
        info: dict[str, dict] = {}
        try:
            res = reader.complete_json(prompts.ILLUSTRATION_SYSTEM, parts)
            for e in res.get("panels", []):
                lab = _norm_label(e.get("id"))
                if lab in labels:
                    info[lab] = {"story": bool(e.get("story", True)), "weight": int(e.get("weight", 3) or 3),
                                 "desc": str(e.get("desc", "")).strip(), "chars": e.get("chars") or []}  # fmt: skip
        except Exception as e:  # noqa: BLE001 - an undescribed episode just offers no stills
            console.print(f"[yellow]  could not describe {ep['title']}: {e}[/]")
            return
        title = f"Anime S{ep['season']}E{ep['episode']:02d} {ep['title']}" + (f" (adapts {ep['adaptation']})" if ep["adaptation"] else "")
        notes = {"cid": cid, "kind": "anime", "title": title, "summary": ep["plot"] or ep["title"], "sig": sig,
                 "labels": {k: p.id for k, p in labels.items()},
                 "panels": {k: v for k, v in info.items() if v["desc"]}}  # fmt: skip
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(json.dumps(notes, ensure_ascii=False, indent=1), "utf-8")
        done[0] += 1
        progress.emit("read", None, None, f"online references: described {done[0]}/{len(work_list)} episodes")

    with ThreadPoolExecutor(max(1, min(4, jobs))) as pool:
        list(pool.map(one, work_list))


def character_refs(work: Path) -> dict[str, Path]:
    """bible name (lower case) -> official character design image, when online references found one."""
    f = web_dir(work) / "chars.json"
    if not f.exists():
        return {}
    return {k.lower(): web_dir(work) / v for k, v in json.loads(f.read_text("utf-8")).items() if (web_dir(work) / v).exists()}


def stills(work: Path) -> dict[str, tuple[Path, set[str]]]:
    """Described anime stills: web panel id -> (image, characters in it, lower case)."""
    rwork = web_dir(work) / ".recaper"
    out: dict[str, tuple[Path, set[str]]] = {}
    for note in sorted((rwork / "notes").glob("c*.json")) if (rwork / "notes").exists() else []:
        n = json.loads(note.read_text("utf-8"))
        idx = rwork / "panels" / n["cid"] / "panels.json"
        if not idx.exists():
            continue
        files = {p["id"]: rwork / p["file"] for p in json.loads(idx.read_text("utf-8"))}
        for lab, info in n["panels"].items():
            pid = n["labels"].get(lab)
            if pid in files and info.get("story", True) and files[pid].exists():
                out[pid] = (files[pid], {c.lower() for c in info.get("chars") or []})
    return out
