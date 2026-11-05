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

- **Library**: every project with its cover, chapter count, and video/working badges.
- **Download**: search a source, pick a language, select chapters (all / range / not downloaded, per-chapter
  scanlation group, preferred group), choose the project folder and image quality. You can **make the video
  automatically when the download finishes**. Re-downloading skips chapters already on disk.
- **Project → Make video**: stop after any stage, redo stages, panel previews, output name, a live progress bar
  per stage, a streaming log, and Stop (kills the whole run, including render/voice workers).
- **Settings**: general options for **every project** (models, language, voice, look, image generator…),
  saved to `~/.recaper.toml`. Per-series options (title, chapters, image sources) stay on the project.
- **Project → Options**: *every* setting (generated from the config, so none are missing), with help text.
  Empty fields show what is inherited (global Settings, else the default). Saved to the project's
  `recaper.toml`, the same file the CLI reads.
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
Downloadable here / Copyright-free), and **NSFW / 18+**. NSFW is off by default; when on, adult works appear
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
4. **Generation for the gaps.** When no image fits a key moment, the writer describes the scene and
   `images.generator` (`gemini` or `openai`) draws it. The work's own art (illustrations, linked panels,
   cover) is sent as style and character references. The budget (`images.max_generated`) is spread across
   chapters, and results are cached. With the generator off or out of budget, each beat uses the closest
   real picture from its chapter. A title card is the last resort.

API keys go in a git-ignored `.env` file in the repo root (or `~/.recaper.env`), e.g. `GEMINI_API_KEY=...`.
Existing environment variables take precedence.

## Generation options

Everything is in the UI's Options tab (and `recaper.toml`). Beyond story, models, voice and video:

- **Look & motion:** background for the motion style (`paper`, `black`, `white`, `dark`, `blur` = blurred
  current panel, or any `#rrggbb`), multi-panel layout (cascade / row), panel reveal (progressive /
  together), card border, shadow strength, camera motion, hit effect, animation speed, frame fill, max
  upscale, background blur and brightness.
- **Story:** cold-open hook on/off, hook length, and a closing line voiced over the last panel.
- **Panel slicing:** manhwa gutter size, minimum panel height, maximum panel ratio, and the manga background
  level. Changing these re-cuts and re-reads only the affected chapters.

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
single call reconciles character names), Kokoro voices in 3 GPU worker processes, and frames render in 8 processes
into NVENC (settled frames are one cached composition + one camera warp).

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

`--reader` must see images; `--writer` only needs text. Each takes a comma-separated **fallback chain**: if a provider hits a usage cap it is skipped for the rest of the run.

| Spec | Auth | Vision |
|---|---|---|
| `claude[:sonnet\|opus\|haiku\|<id>]` (default) | your Claude subscription via the logged-in Claude Code CLI (Agent SDK). If `ANTHROPIC_API_KEY` is set, that key is billed instead. | ✓ |
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
--style motion|blurfill   motion = panel cards on paper, revealed in sync with the narration, cascading (manga default)
                          blurfill = panel over blurred copy, Ken Burns (manhwa default)
--mpc 2.5                 narration minutes per chapter   |  --minutes 90  total length target
--chapters 1-20           chapter selection
--tts kokoro --voice am_michael --speed 1.2   (1.2 ≈ 200 wpm, close to top recap channels)
--extra "Call the MC 'Jinwoo'. Hype tone."   extra writer directions
--until panels|read|script|voice|render      stop early (e.g. review script.json)
--force script            redo a stage (repeatable)
--preview                 write panel contact sheets to .recaper/preview/
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
