Drop manga / manhwa chapters into a folder, run one command, get a narrated recap video.

```
recaper run ./solo-leveling
```

```
./solo-leveling/
  Ch 001.pdf  Ch 002.cbz  Chapter 3/001.jpg …   ← any mix of PDFs, CBZ/ZIP, image folders
  recaper.toml                                   ← optional per-series settings
  solo_leveling_recap.mp4                        ← output
  solo_leveling_recap.chapters.txt               ← YouTube chapter timestamps
  .recaper/                                      ← cache (panels, notes, script, audio, chunks)
```

## Web UI

```
uv run recaper ui                    # opens http://127.0.0.1:8765, projects live in ./library
uv run recaper ui -L D:\recaps       # any library folder (one sub-folder per series)
```

- **Library**: every project with its cover and status (videos / script ready / not started), newest first,
  with live progress on the cover while a video is being made, and a search box once the library grows.
- **Download**: search a source, pick a language, select chapters (all / range / not downloaded, per-chapter
  scanlation group, preferred group), choose the project folder and image quality. You can **make the video
  automatically when the download finishes**. Re-downloading skips chapters already on disk.
- **Project → Make video** walks through four steps, with a summary and the start buttons beside them:
  1. **Chapters**: all by default; "Choose chapters…" opens the list (tick, shift-click a range, or type
     `1-5,8`; cached chapters show as read / script). A subset is saved as `<title>_ch1-5_recap.mp4`, so it
     never overwrites the full video.
  2. **Look**: Auto / Motion / Blur-fill as cards **previewed with a real frame made from the project's own
     panels**, background swatches and resolution (1080p, 720p, 4K, vertical Shorts…).
  3. **Narration**: language, voice (the selected engine's voices, narration-language voices first) with a
     **▶ sample** at the chosen speed, and speed.
  4. **Story & length**: who the narration is written for (Auto / Fans / New viewers), the **intro summary**
     on or off, Short / Standard / Detailed per chapter, and splitting long recaps into parts.

  These quick settings save straight into the project's options. The summary shows the estimated video length
  (calibrated on the project's last narration) and the model calls left to make. **⚡ Quick preview** renders the
  first ~60 s at 480p; **▶ Make video** the whole thing. Stop-after, redo stages, output name and contact sheets
  are under *Advanced*. While it runs: a progress bar per stage, a collapsible log, Stop (kills the whole run,
  including render/voice workers), the progress in the tab title, and a desktop notification when it finishes
  in the background. When it is done the video plays right there, with Download.
- **Queue**: video runs are queued and run one at a time (each one already uses the whole GPU, CPU and model
  quota); the job shows its place in the queue. Downloads always start right away and run in parallel.
- **Chapter viewer** (👁 in the Make and Chapters tabs): read any chapter in the browser, as a webtoon strip or
  page by page (← → or click the page edges, Esc closes). Books show the chapter text with its illustrations.
- **Settings**: general options for **every project** (models, language, voice, look, image generator…),
  saved to `~/.recaper.toml`. Per-series options (title, chapters, image sources) stay on the project.
- **Project → Options**: *every* setting (generated from the config, so none are missing), with help text.
  **Essentials** shows the ~20 that matter most (plus anything set here), **All options** the rest; a search box
  and section links find the others. Empty fields show what is inherited (global Settings, else the default).
  Leaving with unsaved changes asks first. Saved to the project's `recaper.toml`, the same file the CLI reads.
  The same editor runs the global Settings page.
- **Deleting**: the 🗑 on a library card or the project page deletes the whole project, or only its cache
  (re-cut / re-read / re-voice next run) or its videos. Single chapters (Chapters tab) and videos (Videos tab)
  can be deleted too. A running job is stopped first, and a deleted project is unlinked from projects that
  used it as an image source. Deleted chapters can be downloaded again.
- **Project → Script**: edit every beat's text, emphasis and panels (thumbnail picker), reorder/insert/delete,
  see the story bible, then **Save & render** (only changed lines are re-voiced).
- **Project → Videos / Chapters / Panels**: in-browser player, download, copyable YouTube chapters,
  drag-and-drop upload of chapter folders, .cbz or .pdf, and contact sheets of the panel slicing.
- **System check**: AI providers, TTS engines, ffmpeg/NVENC.

The server binds to `127.0.0.1` only. It has no authentication, so don't expose it on a network.

## Downloading chapters

Sources are **connectors** (`src/recaper/sources/`). Each one uses an official or public API. There's no
connector for unofficial aggregator sites, and none that bypasses site protections.

| Source | What it is | Sort | Copyright-free filter | NSFW |
|---|---|---|---|---|
| **MangaDex** | Official public API; scanlations in 15+ languages | popular, rating, recently updated, newest, title | not offered (no licence data; scanlations are not copyright-free) | ✓ |
| **Internet Archive** | Comics, strips and manga on archive.org; chapters are whole PDF/CBZ/ZIP files | popular, rating, recently added, newest, title | **pre-1931 library scans** (US public domain) | no ratings |
| **Pepper&Carrot** | David Revoy's open webcomic, 39 episodes in 70+ languages | n/a | everything (**CC-BY 4.0**, credit required) | n/a |

**Filters in the Download page:** search or browse with an empty query, sort, *Show* (Everything /
Downloadable here / Copyright-free), and **NSFW / 18+**. NSFW is on by default (untick it to hide adult works); adult works appear
with an 18+ label and covers are never blurred.

**Why "copyright-free" is strict:** archive.org licence *and* date fields are typed in by uploaders and are
often wrong: Spider-Man, Berserk and Fables are tagged "public domain", Batman is dated 1340. So the filter only
trusts what a library catalogued: items physically scanned by IA / partner libraries and published before
1931. Pepper&Carrot is published under CC-BY 4.0 by its author. Its credit line shows on the series page and
in the project's Videos tab: paste it into the video description.

**Downloadable here** on MangaDex is verified per series: MangaDex's own flag counts official external links
(that's why licensed titles like Solo Leveling used to slip through), so each result is checked for chapters
actually hosted in your language.

New projects get a `recaper.toml` hint from the source: Long Strip / Korean / Chinese → manhwa, Japanese →
manga right-to-left, Pepper&Carrot and library scans → pages left-to-right.

```
uv run recaper download "berserk of gluttony" -c 1-20 -l en                    # MangaDex into ./library/<title>/
uv run recaper download https://mangadex.org/title/<id> -c 1-5 --video         # then make the video
uv run recaper download pepper --source peppercarrot -l pt -c 1-10             # CC-BY webcomic, Brazilian Portuguese
uv run recaper download https://archive.org/details/<id> --source archive -l "" # an archive.org item
```

Options: `--lang`, `--group` (preferred scanlation group), `--quality data|data-saver`, `--project`,
`--library`, `--pick N` (Nth search result), `--source mangadex|archive|peppercarrot`.

## Light novels & books

Drop **EPUB**, **text PDF** or **TXT** volumes into a project (upload them, or download a book item from a
source). Projects that are mostly prose switch to book mode automatically; you can also set `source = "book"`.

1. **Chapters.** Volumes are split into real chapters using the EPUB table of contents, PDF bookmarks, or
   "Chapter 3" / "Prologue" headings. Unstructured text is cut into `book.split_words` chunks. Front matter
   (title page, copyright, contents) is dropped, but its colour inserts are kept. Scanned PDFs, where every
   page is an image with an OCR text layer, are read as text: only real illustration pages count as art.
2. **Reading.** The writer model digests each chapter's prose into events. The vision model describes the
   book's own illustrations. Prefaces, editor's notes and similar sections are flagged and skipped.
3. **Related works.** In the project's **Images** tab, *Find related works* searches every source and your
   library for adaptations (e.g. the manga of a light novel). Link one, or open a result and download it: it's
   linked automatically. Linked projects are cut and read with their own settings. One model call then
   matches each book chapter to the adaptation chapters showing the same events, and the writer picks
   fitting panels beat by beat.
4. **Online references** (`images.web = "auto"`). The series is looked up online once and cached in
   `.recaper/web/`:
   - **Character designs:** official character art from AniList, matched to the story bible's names. These
     are the identity references for generation.
   - **Anime screenshots:** pulled from every episode gallery of the series' Fandom wiki, with the episode's
     plot and the light-novel volume and chapters it adapts. The wiki is guessed from the title, or set
     `images.wiki` yourself. The vision reader describes the screenshots, about 15 per call. Episodes are
     then matched to book chapters like a linked adaptation, and the writer can pick them as `A*` pictures.
   - **Already-written chapters:** they keep their text. A picture pass gives their picture-less beats a
     fitting screenshot, at one cheap call per chapter.

   For Mushoku Tensei that's 59 episodes, about 1,000 screenshots, and designs for 75 characters.
5. **Generation for the gaps.** When no image fits a key moment, the writer describes the scene and
   `images.generator` draws it.
   - **References:** the official design of every character in the scene, then colour frames from the
     anime episodes of the same arc (right ages and outfits). The book's black-and-white illustrations are
     only a last resort.
   - **Quality check** (`images.quality_check`): the reader scores each image 1-10 against the scene and
     the designs. Images below `images.min_score` (default 7) are redone once with the problems listed. A
     second failure is dropped and remembered, and the beat gets a real picture instead.
   - **Its own stage:** `--until images` (and `--force images` to redraw everything) runs after the script.
     The project's **Generated** tab (book projects) shows every picture request: the image it got, its
     quality-check score and problems, and the line it illustrates. From there you can *Generate missing*,
     *Redraw all*, or select pictures, optionally edit their scene description, and redraw just those. The
     script is never rewritten, and each beat keeps its real picture as a fallback.
   - **Generators:**
   - `antigravity`: your Google AI plan through Antigravity, **no API key**. It uses the agent's built-in
     `generate_image` tool; that tool is the only one recaper allows, everything else is refused. About a minute
     per image, 4 at a time. The tool can't take reference images, so the model describes their style in words:
     the style matches well, faces less exactly than the API generators.
   - `gemini` (`GEMINI_API_KEY`, about $0.04 per image) and `openai` (`OPENAI_API_KEY`) see the references
     directly.

    The budget (`images.max_generated`) is spread across
   chapters, and results are cached. With the generator off or out of budget, each beat uses the closest
   real picture from its chapter. A title card is the last resort.

API keys go in a git-ignored `.env` file in the repo root (or `~/.recaper.env`), e.g. `GEMINI_API_KEY=...`.
Existing environment variables take precedence.

## Generation options

Everything is in the UI's Options tab (and `recaper.toml`). Beyond story, models, voice and video:

- **Look & motion:** background for the motion style (`paper`, `black`, `white`, `dark`, `blur` = blurred
  current panel, or any `#rrggbb`), multi-panel layout (clean = at most two panels, never overlapping / cascade / row), panel reveal (progressive /
  together), card border, shadow strength, camera motion, hit effect, animation speed, frame fill, max
  upscale, background blur and brightness.
- **Story:** intro summary (cold-open hook) on/off, its length, and a closing line voiced over the last panel.
- **Writing style:** the narration is written to sound like a person telling a friend what happened (plain
  words, names instead of re-attached descriptions, the odd aside), with a list of AI tells it must avoid
  ("little did", "a testament to", "fragile calm shatters"…). The polish pass checks for them too.
- **Viewers already know the story** (`known_story`: auto / yes / no): for later arcs, the script doesn't
  introduce characters or re-explain backstory, powers and terms; it writes for fans and only explains what is
  new. *auto* turns it on when the chosen chapters don't start at the series' beginning (a project starting at
  "Ch 337", or picking chapters 50+). Changing it, or a new writing style, rewrites the chapter scripts; panel
  reading is kept.
- **Polish pass** (`polish`, on by default): after writing, one extra call per chapter line-edits the
  script, by the writer or by a separate **polisher** model (`polisher`, CLI `--polisher claude:sonnet`): a
  local writer drafts, a stronger model edits. Its calls show on their own row of the usage estimate. A local checker first lists what to fix: speech reported in the present ("he tells her"), sentences
  that keep opening with the same word, repeated phrases, too few "but / however" turns, sentences over 35
  words, more than one rhetorical question. The model also checks names against the story bible. Panels,
  emphasis and beat count never change, and a rewritten line that lost or invented too much is ignored.
  Results are cached.
- **Parts** (`episode_minutes`, CLI `--parts 15`): long recaps become `_part01.mp4`, `_part02.mp4`… made of
  whole chapters. A short last stretch joins the previous part. Parts 2+ open with a ~25 s "previously on"
  (`previously`) written from the earlier parts' script and illustrated with their key panels.
- **Quick preview** (UI ⚡ button, CLI `--quick`): the first ~60 s at 480p, rendered in seconds, to judge
  look, voice and pacing. Its audio is reused by the full run.
- **Panel upscaler** (`render.upscaler`, default `auto`): panels shown enlarged are upscaled once with
  Real-ESRGAN (anime model, CUDA GPU, ~0.1 s per panel, cached in `.recaper/upscaled/`), so they fill the
  frame without going soft. `fast` looks natural; `quality` gives crisper lines but can over-sharpen. Needs
  `uv sync --extra kokoro` (or `--extra upscale`); the model (2.5 MB) downloads on first use.
- **Panel slicing:** manhwa gutter size, minimum panel height, maximum panel ratio, and the manga background
  level. Changing these re-cuts and re-reads only the affected chapters.

## Usage & cost

Every run logs its model calls (per provider: ok / quota hit / refused / error, panel images sent, text in
and out, time), generated images, newly voiced lines, upscaled panels and renders to `.recaper/usage.jsonl`.
The project's **Usage** tab shows each run. It also shows an estimate of the next full run, counting only
what isn't cached: reader calls and panel images, writer calls, the most images that could be generated
(with a $ estimate for API generators), voice words, parts. Providers are tagged *plan / free*
(Claude or Antigravity subscriptions, Gemini CLI, OpenCode free, Ollama) or *API-billed* (API keys). The Make
tab shows the same estimate in one line.

## Pipeline

| Stage | What happens | Cached in `.recaper/` |
|---|---|---|
| **panels** | Manhwa strips are stitched and cut at gutters (paper-coloured, constant-colour gaps; gradient skies are not gutters). Manga pages are segmented by flood-filling the page background and ordered with an XY-cut (right-to-left by default). PDFs with one image per page are extracted at native resolution. | `panels/`, `preview/` |
| **read** | A **vision** model reads every panel in order (tall panels are tiled so text stays legible) and writes per-panel notes, a chapter summary and a running **story bible** (characters, aliases, terms) so names stay consistent. Credits / ads / title cards are flagged as non-story. | `notes/`, `bible.json` |
| **script** | A **writer** model turns notes into narration *beats* (1–3 sentences + the panels to show + emphasis), then writes a cold-open hook. Style is modelled on the best reference recaps: storyteller voice, present tense, interpretation over transcription, a "but/however" turn every few beats, cliffhanger outro. | `script/`, **`script.json`** |
| **voice** | Every beat is synthesised with [RealtimeTTS](https://github.com/KoljaB/RealtimeTTS) (any engine). Audio is cached per text, so editing a line only re-voices that line. | `audio/`, `narration.wav` |
| **render** | Frames are composited in parallel processes and piped to ffmpeg (NVENC when available), then muxed with the narration (high-pass, compression, loudness-normalised to −16 LUFS) and chapter markers. | `render/` |

Every stage resumes from cache. Edit `.recaper/script.json` (text, panels, emphasis) and re-run to re-voice and re-render only what changed.

**Speed** (5 chapters / ~300 panels → 16 min video, RTX 5070, Claude via subscription): about 3½ minutes from scratch.
Chapters are read and written in parallel (`--jobs`, default 6; chapter 1 is read first to seed the cast, then a
single call reconciles character names), and each chapter is polished as soon as it is written. Kokoro voices in
3 GPU worker processes; a video split into parts is voiced in one batch, so the model loads once.

Rendering, on an RTX 5070 at 1080p30: blurfill **~22x real time** (~600 fps), motion **~17x** (~510 fps). Frames
are composed **on the GPU** (`render/gpu.py`). The camera warp, cards, shadows, cross-fades and the I420
conversion all run on the device, and each worker only downloads finished 3 MB frames and pipes them to NVENC
(preset p1, which measured the same quality as p3 at CQ 21). The 8 render workers start while the narration is
voiced and are reused across parts. Chunks are 40 s, and the audio is mastered while the video renders. Without
CUDA the same frames are composed on the CPU, about 2x slower; `RECAPER_GPU_WARP=0` forces that path.

**Quota:** reading sends every panel image to the reader model. A long series can use up a Claude plan's 5-hour
window; recaper detects the cap, falls back to the next provider in the chain, or stops with the reset time. All
finished work is cached, so re-running later resumes where it stopped.

## Install

Requires Python 3.10–3.12, [uv](https://docs.astral.sh/uv/) and ffmpeg on PATH.

```
uv sync --extra kokoro      # local GPU voice (CUDA torch); or --extra edge / coqui / elevenlabs / openai-tts
uv run recaper doctor --live
```

## Models

`--reader` must see images; `--writer` and `--polisher` (the polish pass, default: the writer) only need text. Each takes a comma-separated **fallback chain**: if a provider hits a usage cap it is skipped for the rest of the run.

| Spec | Auth | Vision |
|---|---|---|
| `claude[:sonnet\|opus\|haiku\|<id>]` (default) | your Claude subscription via the logged-in Claude Code CLI (Agent SDK). If `ANTHROPIC_API_KEY` is set, that key is billed instead. | ✓ |
| `antigravity[:model]` (alias `agy`) | your Google AI subscription via the local Antigravity ACP server (e.g. `antigravity:gemini-4-argon-high`, `antigravity:gemini-3.8-flash-high`). Run `uv run python -m recaper.llm.antigravity` to list the models on your plan. | ✓ |
| `gemini[:model]` | `GEMINI_API_KEY` if set, otherwise the logged-in `gemini` CLI. Force with `gemini-api` / `gemini-cli`. | ✓ |
| `openai[:model]` | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` for compatible servers) | ✓ |
| `ollama[:model]` | local Ollama (default `qwen2.5vl:7b`) | ✓ (VL models) |
| `deepseek-free` = `opencode:opencode/deepseek-v4.1-flash-free` | OpenCode's free tier, driven through the `opencode` CLI with a tool-less agent | ✗ text only |

About **DeepSeek V4.1 Flash Free**: OpenCode's free models only work from inside OpenCode, so recaper drives `opencode run` headlessly. The free endpoint rejects images, and the free quota is shared across all OpenCode free models (resets about a day after it runs out). Use it as a writer with a fallback:

```
recaper run ./series --reader claude --writer deepseek-free,claude
```

## Common options

```
--lang pt                 narration language (script + voice), default en
--style motion|blurfill   motion = panel cards on paper, revealed in sync with the narration, at most two at a time (manga default)
                          blurfill = panel over blurred copy, Ken Burns (manhwa default)
--mpc 2.5                 narration minutes per chapter   |  --minutes 90  total length target
--chapters 1-20           chapter selection
--tts kokoro --voice am_michael --speed 1.0   (default; 1.0 ≈ 170 wpm, 1.2 ≈ 200 wpm like fast recap channels)
--extra "Call the MC 'Jinwoo'. Hype tone."   extra writer directions
--until panels|read|script|voice|render      stop early (e.g. review script.json)
--force script            redo a stage (repeatable)
--preview                 write panel contact sheets to .recaper/preview/
--quick                   quick preview: first ~60 s at 480p -> <title>_recap_preview.mp4
--parts 15                split into ~15-minute parts with a "previously on" intro
--jobs 6                  parallel model calls for reading / writing
--fps 60 --resolution 2560x1440 --encoder x264 --workers 8
```

`recaper script ./series` prints the current script with its panel numbers.

## recaper.toml

Settings are layered, later wins: built-in defaults < **global settings** `~/.recaper.toml` (the UI's
Settings page; `RECAPER_SETTINGS` points elsewhere) < `recaper.toml` in the current directory <
`recaper.toml` in the series folder < CLI flags. Put this in the series folder (or the global file) to avoid
repeating flags:

```toml
lang = "en"
reader = "claude:sonnet"
writer = "deepseek-free,claude:sonnet"
minutes_per_chapter = 2.5
extra_instructions = "Keep the tone hype but not cringe."

[tts]
engine = "kokoro"
voice = "am_michael"
speed = 1.2
workers = 3          # parallel TTS processes (local engines)

[render]
style = "auto"
fps = 30
```

## Tests

```
uv run pytest
uv run python tests/fixtures.py samples   # synthetic manga + manhwa for a quick end-to-end run
uv run recaper run samples/manga --mpc 0.6
```
