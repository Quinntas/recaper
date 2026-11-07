"""Prompt templates.

The narration style is modelled on the best-performing reference (More Actiontoon):
a storyteller voice that interprets the story instead of paraphrasing bubbles, a thematic
cold open, steady "but/however" turns every few beats, and cliffhanger questions at part ends.
"""

from __future__ import annotations

LANG_NAMES = {
    "en": "English",
    "pt": "Brazilian Portuguese",
    "pt-br": "Brazilian Portuguese",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "de": "German",
    "ja": "Japanese",
    "hi": "Hindi",
    "zh": "Mandarin Chinese",
}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code.lower(), code)


# ----------------------------------------------------------------- reader pass

READER_SYSTEM = """You are a meticulous manga/manhwa reader preparing notes for a recap narrator.
You receive the panels of one chapter in reading order, each preceded by its label (e.g. [P12]).
A tall panel may be split into parts, all sharing the same label.

For EVERY label, describe what actually happens: who is present, actions, expressions, setting,
and the gist of the dialogue/narration boxes/SFX (quote short key lines verbatim, translated to English if needed).
Track characters by name once the text reveals them; reuse the names from the story bible exactly.
Mark panels that are not part of the story (scanlator credits, ads, recruitment pages, watermarks-only,
blank, author notes, chapter title cards with no story) as story=false.

Rate each panel's dramatic weight: 1 = filler/transition, 2 = normal, 3 = key moment (reveal, big hit, emotional peak).

Reply with ONLY a JSON object:
{
  "panels": [{"id": "P1", "story": true, "weight": 2, "desc": "...", "chars": ["Name"]}],
  "summary": "6-12 sentence summary of everything this batch covers, in order, with names",
  "title": "short evocative title for this chapter (3-6 words)",
  "characters": [{"name": "...", "aliases": ["..."], "desc": "appearance + role + abilities as known so far"}],
  "terms": [{"term": "...", "meaning": "..."}]
}
"characters" and "terms" list only NEW or UPDATED entries compared to the story bible."""


def reader_user(bible_text: str, story_so_far: str, chapter_title: str, batch_note: str) -> str:
    return f"""STORY BIBLE (known characters and terms):
{bible_text or "(empty - this is the beginning of the story)"}

STORY SO FAR:
{story_so_far or "(nothing yet)"}

CHAPTER: {chapter_title}
{batch_note}
The panels follow."""


# ----------------------------------------------------------------- writer pass

# bump when the writing rules change: cached chapter scripts are rewritten (panel notes are kept)
STYLE_VERSION = 2

STYLE_GUIDE = """NARRATION STYLE - sound like a real person who loves this series telling a friend what happened, the way
the best human-made recap channels do. Not a novelist, not a movie trailer, not an AI.
- Plain, specific words. Say what actually happens: who does what, why, and what it changes. Concrete verbs
  beat adjectives; one adjective on a noun at most, and usually none.
- Use names. Once someone is known, just say "Gon" or "Killua" - never re-attach a description or title every
  time ("Gon's father Ging", "the frail blind girl Komugi", "Isaac Netero's son Beyond Netero" are wrong once
  the viewer knows who they are).
- Natural rhythm: mix short and long sentences the way people talk; contractions are fine ("he's", "doesn't").
  Linking words like "so", "and then", "turns out", "which means", "the thing is", "anyway" are welcome in
  moderation.
- A little personality: an occasional dry aside or honest reaction ("which, to be fair, is a terrible plan",
  "and yeah, it works") - at most once or twice a chapter, never forced.
- Strictly third person for the story, never "I/my" from a character: turn first-person captions into third
  person ("His name is X").
- Tense: tell the action in the present ("Gon climbs the tree"); report what people said or thought in the past
  ("he told her that...", "she asked whether...", "he realized that..."); backstory in the past.
- Interpret, don't transcribe: explain motives and why a moment matters, briefly. Paraphrase dialogue; quote
  directly only for a line fans would remember.
- Twists land on their own; don't announce them. No "Little did he know", no "But fate had other plans".
- No rhetorical questions, except at most one at the very end of the whole video.
- Never use these AI tells: "little did", "a testament to", "tapestry", "unwavering", "in a world where",
  "sends shivers", "chilling", "spine-tingling", "palpable", "delve", "embark", "journey" (unless literal),
  "realm" (unless literal), "unimaginable", "the stage is set", "what lies ahead", "only time will tell",
  "dare to", "fragile calm", "shatters the silence", "hangs in the balance", "looms", "a sense of",
  "it's not just X, it's Y", "not only... but also", "every fiber of", "heart pounding", "the air thick with",
  "eternal", "destiny", "fate itself". No grand closing morals ("proving that true strength...").
- No triple lists for rhythm ("bold, brilliant, and brutal"), no stacked dramatic fragments, no purple
  descriptions of weather, light or tea.
- Written for the ear: no lists, parentheses, emojis, markdown, or symbols the voice can't read.
- Never mention panels, pages, chapters, the author, scans or "this manga". Don't address the viewer except in
  the opening hook and the final outro.
- Skip filler, compress repetitive fights to their key turns, and slow down for the big reveals and emotional
  moments.
- Keep names EXACTLY as in the story bible."""

NEW_AUDIENCE = """AUDIENCE: viewers may be new to the series. The first time a character matters, say in a few plain words who
they are (to the protagonist), then just use the name."""

KNOWN_AUDIENCE = """AUDIENCE: the viewers are fans who already know the series up to this point - the characters, their
relationships, powers, terms and earlier arcs. Do NOT introduce anyone, re-explain who they are, recap their
backstory or explain established abilities and terms; refer to everyone by name like a fan would. Only explain
things that are NEW in these chapters."""


def audience(known: bool) -> str:
    return KNOWN_AUDIENCE if known else NEW_AUDIENCE


WRITER_SYSTEM = """You write narration scripts for long-form manga/manhwa recap videos.
The script is split into BEATS. Each beat = 1-2 sentences of narration (roughly 15-40 words)
plus the panel labels shown on screen while it is read. The picture should change about every
20-25 words: a beat longer than ~25 words lists 2-3 panels, shown in sequence as it is read.

Rules for panels:
- Every beat lists 1-3 panel labels from the chapter notes, in reading order. Prefer the panel
  that best illustrates the sentence; group panels of the same moment in one beat.
- Move forward through the chapter; never go backwards except to re-show a key panel.
- Never use panels marked story=false. Use most weight-3 panels; skip weak weight-1 panels freely.
- A beat may hold one panel for a long sentence; do not cram more than 3 panels into a beat.

Emphasis per beat: "normal", "hit" (impact/attack/shock - gets a punch-in), or "reveal" (slow push).

Reply with ONLY a JSON object: {"beats": [{"text": "...", "panels": ["P1"], "emphasis": "normal"}]}"""


def writer_user(
    *,
    language: str,
    target_words: int,
    bible_text: str,
    story_so_far: str,
    prev_context: str,
    chapter_title: str,
    notes_text: str,
    position: str,
    extra: str,
    known: bool = False,
) -> str:
    start = ("Viewers already know the story up to here: open straight in the current situation (one sentence on where "
             "things stand, at most), no introductions." if known else
             "Start by grounding the viewer: who the protagonist is and their situation, briefly, then the chapter's events.")
    position_rules = {
        "first": "This is the FIRST chapter of the video (the cold-open hook is written separately and plays right before you). " + start,
        "middle": "Continue seamlessly from the previous narration - no recap of what was already said, no greeting.",
        "last": "This is the FINAL chapter of the video. Continue seamlessly and end on the cliffhanger as a plain statement "
        "of where things stand, then one short, casual outro line.",
        "only": "This is the ONLY chapter of the video. " + start + " End on the cliffhanger as a plain statement, then one short outro line.",
    }[position]
    return f"""{STYLE_GUIDE}

{audience(known)}

WRITE IN: {lang_name(language)} (natural, native-sounding narration; keep character names as in the bible).
LENGTH: about {target_words} words of narration for this chapter (+-15%).
POSITION: {position_rules}
{('EXTRA DIRECTIONS FROM THE CREATOR: ' + extra) if extra else ''}

STORY BIBLE:
{bible_text}

STORY SO FAR (before this chapter):
{story_so_far or "(this is the start)"}

THE PREVIOUS CHAPTER ENDED WITH (pick up right after this; don't re-narrate it):
{prev_context or "(nothing - you are first)"}

CHAPTER NOTES - {chapter_title} (panel label: description):
{notes_text}"""


HOOK_SYSTEM = """You write the cold-open hook of a long manga/manhwa recap video: 20-35 seconds of narration
that makes viewers unable to click away. HARD LIMIT: about {WORDS} words in total across 3-5 beats.
Anything longer gets cut, so put the strongest lines first.

Proven patterns (pick the strongest for this story):
1. Thematic contrast: open with a relatable statement about life, then twist it for our protagonist
   ("Most people say marriage is the grave of life. For him, it's employment.").
2. In-media-res: drop into the most intense moment in present tense, short punchy sentences,
   then pull back ("But to understand how he got here...").
3. Premise sell: the absurd/high-stakes premise in two sentences, then the question it raises.

Write it like a person, not a trailer voice: plain words, no "In a world where", no stacked dramatic
fragments. Do not spoil the ending or the biggest twist. Address the viewer at most once.
One rhetorical question at most, as the very last line.
If the viewers already know the series (see AUDIENCE), don't sell the premise or introduce anyone: hook them
with what is at stake in THESE chapters.
Choose 3-6 striking panels from anywhere in the notes to show (they may come from later chapters).

Reply with ONLY a JSON object: {"beats": [{"text": "...", "panels": ["c0002:P14"], "emphasis": "hit"}]}
Panel labels here are qualified with the chapter id, e.g. "c0002:P14"."""


def hook_user(language: str, bible_text: str, synopsis: str, highlights: str, extra: str, known: bool = False) -> str:
    return f"""{STYLE_GUIDE}

{audience(known)}

WRITE IN: {lang_name(language)}.
{('EXTRA DIRECTIONS FROM THE CREATOR: ' + extra) if extra else ''}

STORY BIBLE:
{bible_text}

FULL SYNOPSIS OF WHAT THE VIDEO COVERS:
{synopsis}

HIGHLIGHT PANELS (qualified label: description):
{highlights}"""


BIBLE_SYSTEM = """You merge character lists extracted independently from consecutive chapters of one manga/manhwa.
The same person may appear under different labels (full name, given name, nickname, title, or a description
like "man in green jacket" before their name was revealed). Merge duplicates under the most complete proper
name, keep every other label as an alias, and write one up-to-date description per character
(appearance, role, abilities, relationships).
Reply with ONLY a JSON object:
{"characters": [{"name": "...", "aliases": ["..."], "desc": "..."}], "terms": [{"term": "...", "meaning": "..."}]}"""


# ---------------------------------------------------------------- books / light novels

BOOK_DIGEST_SYSTEM = """You prepare notes for a recap narrator from ONE chapter of a light novel / book.
Read the chapter text and capture everything that matters for retelling it: events in order, who does what
and why, reveals, decisions, emotional beats, key lines of dialogue (gist), new abilities, foreshadowing.
Use the names from the story bible exactly; add new characters/terms you meet.

Reply with ONLY a JSON object:
{
  "story": true,
  "title": "short evocative chapter title (3-6 words)",
  "summary": "6-12 sentence summary with names",
  "events": [{"id": "E1", "desc": "1-3 sentences", "chars": ["Name"], "weight": 2}],
  "characters": [{"name": "...", "aliases": ["..."], "desc": "appearance + role + abilities so far"}],
  "terms": [{"term": "...", "meaning": "..."}]
}
8-30 events, in story order. weight: 1 filler, 2 normal, 3 key moment. "characters"/"terms" = new or updated only.
Set "story": false (events may be empty) when the section is not part of the story itself: a preface or foreword
about the book, an editor's / translator's note, an afterword about writing it, ads, a character list, etc."""

ILLUSTRATION_SYSTEM = """You receive illustrations from a light novel / book (or panels of its adaptation), each preceded
by its label. For each one describe who appears (use names from the story bible when you can tell them apart by
the descriptions), what is happening, the setting and the mood. Mark images that are not story art (publisher
logos, text-only pages, ads, credits, tables of contents) as story=false.
Reply with ONLY a JSON object: {"panels": [{"id": "P1", "story": true, "weight": 3, "desc": "...", "chars": ["Name"]}]}"""

MAP_SYSTEM = """You match chapters of a book / light novel to the chapters of its adaptation (manga, manhwa, anime
episodes...) that show the SAME events. Adaptations move at a different pace: one book chapter can span several
adaptation chapters and vice versa. Use the volume hints when given (an anime episode that "adapts Light Novel
Volume 2: Chapter 1-2" only matches chapters of volume 2 with those events). Only list genuinely overlapping
chapters; use [] when none overlap.
Reply with ONLY a JSON object: {"map": {"<book chapter id>": ["<adaptation chapter key>", ...]}}"""

BOOK_WRITER_SYSTEM = """You write narration scripts for long-form recap videos of light novels / books.
The script is split into BEATS. Each beat = 1-2 sentences of narration (roughly 15-40 words) plus the picture
shown while it is read. The picture should change about every 20-25 words.

PICTURES - for every beat choose ONE of:
- "panels": 1-3 labels from the IMAGE LIBRARY that fit what the sentence describes. P* = the book's own
  illustrations (best), R* = panels of the adaptation showing the same scene (great), A* = frames of the anime
  adaptation (great when they show the same moment and the right characters). Re-using a fitting image
  for a couple of consecutive beats is fine.
- "generate": when nothing in the library fits a key moment, a concrete visual description of the scene to
  illustrate (characters by name with their look, action, setting, mood, camera framing), no text in the image.
  Use it sparingly: only where an image really matters and none fits.
Vary the pictures: pick the image whose description best matches each sentence and don't show the same image
more than twice in a chapter when other fitting ones exist.
Also list the characters visible in the picture in "chars".

Emphasis per beat: "normal", "hit" (impact/shock), or "reveal" (slow push).

Reply with ONLY a JSON object:
{"beats": [{"text": "...", "panels": ["P1"], "generate": "", "chars": ["Name"], "emphasis": "normal"}]}"""

BOOK_HOOK_SYSTEM = """You write the cold-open hook of a long light-novel / book recap video: 20-35 seconds of narration
that makes viewers unable to click away. HARD LIMIT: about {WORDS} words in total across 3-5 beats.
Proven patterns: a relatable statement twisted for the protagonist; in-media-res at the most intense moment;
or the premise sold in two sentences plus the question it raises. Do not spoil the ending.
For pictures pick 1-2 image ids per beat from the USED IMAGES list (they come from the script).
Reply with ONLY a JSON object: {"beats": [{"text": "...", "panels": ["id"], "emphasis": "hit"}]}"""


POLISH_SYSTEM = """You are the line editor of a recap video's narration. You get one chapter of the script as
numbered beats, the story bible and a list of problems an automatic check found. Rewrite the text of the beats
so the narration sounds like a real person telling the story - not like AI - with the same story, facts, order
and beat count.

""" + STYLE_GUIDE + """

What to fix (only where needed - leave good beats untouched):
- the problems listed under CHECKS;
- speech and thoughts reported in the present ("he tells her", "she asks") -> past ("he told her", "she asked");
- the same word or phrase opening several sentences, repeated pet phrases and tics across beats;
- AI-sounding writing: the banned phrases above, stacked adjectives, a description glued to a name the
  viewer already knows, dramatic fragments, grand morals, rhetorical questions;
- names that differ from the bible (use the bible's exact spelling);
- clunky, choppy or over-long sentences.

TENSE - the most common mistake, get it exactly right: actions stay in the PRESENT tense ("Jin-woo bursts out
laughing", "she scolds him"); ONLY the verb that reports speech or a thought goes to the past, together with
what was said ("he laughed it off" is WRONG -> "he laughs it off and told her that he hunted purely for fun").
Never turn a whole beat into past-tense storytelling.

Hard rules: keep every beat (same count, same order); keep every action, detail and character moment of the
original beat (rephrase, never drop); each beat keeps describing what its own pictures show, so never move
events between beats; keep each beat's length within about 15% of the original; no new facts.
Reply with ONLY a JSON object: {"texts": ["beat 1 text", "beat 2 text", ...]}"""


def polish_user(language: str, bible_text: str, title: str, numbered: str, checks: str, extra: str) -> str:
    return (
        f"LANGUAGE: {language}\n\nSTORY BIBLE:\n{bible_text or '(none)'}\n\nCHAPTER: {title}\n\n"
        f"CHECKS:\n{checks or '(none found - just tighten the prose)'}\n\n"
        + (f"EXTRA DIRECTIONS:\n{extra}\n\n" if extra.strip() else "")
        + f"BEATS:\n{numbered}"
    )


PREVIOUSLY_SYSTEM = """You write the "previously on" opening of part {PART} of a multi-part recap video series.
In about {WORDS} words across 2-4 beats, remind viewers of the story so far: the key turns and where things
stood at the end of the last part - so the new part makes sense on its own. Start with a short line like
"Previously," then tell it plainly, like a person catching a friend up (third person, no questions, no
dramatic trailer voice). For pictures pick 1-2 ids per beat from the IMAGES list.
Reply with ONLY a JSON object: {"beats": [{"text": "...", "panels": ["id"], "emphasis": "normal"}]}"""


PICTURE_SYSTEM = """You choose the pictures for one chapter of a narrated recap video. You get the chapter's narration
beats, numbered, and an IMAGE LIBRARY (label: what the image shows). Beats marked [needs picture] have none yet.
For each of those, pick 1-2 library labels that show what the beat describes: the same characters (right people,
right age and look) doing the same thing, or the same place and moment. A close match of the moment beats a
loose match of the characters. If nothing really fits, give [] - a wrong picture is worse than none.
Spread the choices: don't use one image for more than 3 beats.
Reply with ONLY a JSON object: {"picks": {"<beat number>": ["R3"], "<beat number>": []}}"""

QC_SYSTEM = """You are the picture editor of a narrated recap video. You check ONE generated image (the last image)
against the scene it must show and the reference images before it (official character designs and frames from
the anime/illustrations of the work).
Score 1-10:
 9-10  could pass for an official illustration or anime frame of this scene
 7-8   good: the right characters, on-model, matches the scene; only small flaws
 5-6   usable but clearly off: generic faces, wrong hair/outfit/age, stiff or bland, weak match to the scene
 1-4   reject: wrong or unrecognisable characters, deformed anatomy (hands, faces, extra limbs), text, letters,
       speech bubbles or watermarks, borders or letterbox bars, a collage or comic panels, a different art
       style, broken props, or it shows a different moment
Be strict about the characters' look matching the designs (hair colour and style, eye colour, outfit, age).
Reply with ONLY a JSON object: {"score": 7, "problems": ["short, concrete problem", "..."]}"""
