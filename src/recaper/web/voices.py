"""Voice picker for the UI: the voices each TTS engine offers, and short spoken samples to preview them.

Samples are synthesised in one warm worker process (the TTS model loads once, ~10 s for Kokoro, and stays out of
the UI server's own memory); it shuts down after a few idle minutes. Samples are cached by engine/voice/speed/text.
"""

from __future__ import annotations

import json
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..tts import DEFAULT_VOICES, ENGINE_CLASSES

KOKORO_VOICES = [
    "am_michael", "am_fenrir", "am_puck", "am_adam", "am_echo", "am_eric", "am_liam", "am_onyx", "am_santa",
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_nova", "af_aoede", "af_kore", "af_jessica",
    "bm_george", "bm_fable", "bm_lewis", "bm_daniel", "bf_emma", "bf_isabella", "bf_alice", "bf_lily",
    "pm_alex", "pm_santa", "pf_dora", "em_alex", "em_santa", "ef_dora", "ff_siwis", "im_nicola", "if_sara",
    "hm_omega", "hm_psi", "hf_alpha", "hf_beta", "jm_kumo", "jf_alpha", "zm_yunjian", "zm_yunxi", "zf_xiaobei",
]  # fmt: skip
KOKORO_LANG = {"a": ("en", "American English"), "b": ("en", "British English"), "p": ("pt", "Portuguese"),
               "e": ("es", "Spanish"), "f": ("fr", "French"), "i": ("it", "Italian"), "h": ("hi", "Hindi"),
               "j": ("ja", "Japanese"), "z": ("zh", "Chinese")}  # fmt: skip
EDGE_VOICES = [
    ("en-US-AndrewMultilingualNeural", "Andrew (US, male, warm narrator)"), ("en-US-BrianMultilingualNeural", "Brian (US, male)"),
    ("en-US-ChristopherNeural", "Christopher (US, male, deep)"), ("en-US-GuyNeural", "Guy (US, male)"),
    ("en-US-EricNeural", "Eric (US, male)"), ("en-US-AvaMultilingualNeural", "Ava (US, female)"),
    ("en-US-EmmaMultilingualNeural", "Emma (US, female)"), ("en-US-JennyNeural", "Jenny (US, female)"),
    ("en-GB-RyanNeural", "Ryan (UK, male)"), ("en-GB-SoniaNeural", "Sonia (UK, female)"), ("en-AU-WilliamNeural", "William (AU, male)"),
    ("pt-BR-AntonioNeural", "Antonio (Brazil, male)"), ("pt-BR-FranciscaNeural", "Francisca (Brazil, female)"),
    ("es-MX-JorgeNeural", "Jorge (Mexico, male)"), ("es-ES-AlvaroNeural", "Álvaro (Spain, male)"),
    ("fr-FR-HenriNeural", "Henri (France, male)"), ("de-DE-ConradNeural", "Conrad (Germany, male)"),
    ("it-IT-DiegoNeural", "Diego (Italy, male)"), ("ja-JP-KeitaNeural", "Keita (Japan, male)"),
    ("zh-CN-YunxiNeural", "Yunxi (China, male)"), ("hi-IN-MadhurNeural", "Madhur (India, male)"),
]  # fmt: skip
OPENAI_VOICES = ["onyx", "echo", "ash", "ballad", "verse", "fable", "alloy", "coral", "nova", "sage", "shimmer"]

SAMPLES = {
    "en": "Gon stood at the edge of the forest. He had no idea the hunt had already begun - and that he was the prey.",
    "pt": "Gon parou na beira da floresta. Ele não fazia ideia de que a caçada já tinha começado - e de que a presa era ele.",
    "es": "Gon se detuvo al borde del bosque. No sabía que la cacería ya había comenzado, y que la presa era él.",
    "fr": "Gon s'arrêta à l'orée de la forêt. Il ignorait que la chasse avait déjà commencé, et que la proie, c'était lui.",
    "it": "Gon si fermò al limitare della foresta. Non sapeva che la caccia era già iniziata, e che la preda era lui.",
    "de": "Gon blieb am Waldrand stehen. Er ahnte nicht, dass die Jagd längst begonnen hatte - und dass er die Beute war.",
    "ja": "ゴンは森の入り口で立ち止まった。狩りがすでに始まっていて、獲物が自分だとは知らずに。",
    "hi": "गॉन जंगल के किनारे रुका। उसे नहीं पता था कि शिकार शुरू हो चुका है - और शिकार वही है।",
    "zh": "小杰停在森林边缘。他还不知道，狩猎早已开始，而猎物正是他自己。",
}  # fmt: skip


def _label(v: str) -> str:
    return f"{v.split('_', 1)[1].title()} ({'male' if v[1] == 'm' else 'female'})"


def catalog(engine: str, lang: str) -> dict:
    """{voices: [{value, label, group, match}], default, free_text}: match = speaks the narration language."""
    engine = (engine or "kokoro").lower()
    base = (lang or "en").split("-")[0].lower()
    default = DEFAULT_VOICES.get(engine, {}).get(lang) or DEFAULT_VOICES.get(engine, {}).get(base)
    out: list[dict] = []
    if engine == "kokoro":
        for v in KOKORO_VOICES:
            code, name = KOKORO_LANG.get(v[0], ("?", v[0]))
            out.append({"value": v, "label": _label(v), "group": name, "match": code == base})
    elif engine == "edge":
        for v, label in EDGE_VOICES:
            out.append({"value": v, "label": label, "group": v.split("-")[0], "match": v.lower().startswith(base)})
    elif engine == "openai":
        out = [{"value": v, "label": v.title(), "group": "OpenAI", "match": True} for v in OPENAI_VOICES]
    else:
        for v in sorted(set(DEFAULT_VOICES.get(engine, {}).values())):
            out.append({"value": v, "label": v, "group": engine.title(), "match": True})
    out.sort(key=lambda x: (not x["match"], x["group"]))
    return {"engine": engine, "voices": out, "default": default, "free_text": engine not in ("kokoro",),
            "known_engine": engine in ENGINE_CLASSES}  # fmt: skip


# ------------------------------------------------------------------ samples (warm worker process)

SAMPLE_DIR = Path(tempfile.gettempdir()) / "recaper-voice-samples"
_VOICES: dict = {}


def _worker_init() -> None:
    import warnings

    warnings.filterwarnings("ignore")


def _synth(engine: str, voice: str | None, speed: float, lang: str, args: dict, text: str) -> str:
    from ..config import TTSConfig
    from ..tts import Voice

    cfg = TTSConfig(engine=engine, voice=voice or None, speed=speed, args=args or {})
    key = json.dumps([engine, voice, speed, args, lang], sort_keys=True)
    v = _VOICES.get(key)
    if v is None:
        if len(_VOICES) > 4:  # keep a few engines/voices loaded, not all of them
            _VOICES.clear()
        v = _VOICES[key] = Voice(cfg, lang, SAMPLE_DIR)
    return str(v.synth(text))


class Sampler:
    IDLE = 300.0  # seconds before the worker (and its model) is released

    def __init__(self) -> None:
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _shutdown(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None

    def sample(self, engine: str, voice: str | None, speed: float, lang: str, args: dict | None = None, text: str | None = None) -> Path:
        base = (lang or "en").split("-")[0]
        text = (text or SAMPLES.get(base) or SAMPLES["en"]).strip()[:400]
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._timer:
                self._timer.cancel()
            if self._pool is None:
                self._pool = ProcessPoolExecutor(max_workers=1, initializer=_worker_init)
            fut = self._pool.submit(_synth, engine, voice, float(speed), lang, args or {}, text)
        try:
            return Path(fut.result(timeout=300))
        finally:
            with self._lock:
                self._timer = threading.Timer(self.IDLE, self._shutdown)
                self._timer.daemon = True
                self._timer.start()

