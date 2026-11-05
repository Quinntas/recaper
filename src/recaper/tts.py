"""Voice-over through RealtimeTTS (any of its engines), one cached WAV per beat."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import TTSConfig
from .log import console

# engine short name -> RealtimeTTS class
ENGINE_CLASSES = {
    "kokoro": "KokoroEngine",
    "edge": "EdgeEngine",
    "coqui": "CoquiEngine",
    "xtts": "CoquiEngine",
    "openai": "OpenAIEngine",
    "elevenlabs": "ElevenlabsEngine",
    "azure": "AzureEngine",
    "system": "SystemEngine",
    "gtts": "GTTSEngine",
    "orpheus": "OrpheusEngine",
    "chatterbox": "ChatterboxEngine",
    "piper": "PiperEngine",
    "cartesia": "CartesiaEngine",
    "minimax": "MiniMaxEngine",
    "qwen": "QwenEngine",
}

# sensible male narrator defaults per engine/language (override with --voice)
DEFAULT_VOICES = {
    "kokoro": {"en": "am_michael", "pt": "pm_alex", "pt-br": "pm_alex", "es": "em_alex", "fr": "ff_siwis",
               "it": "im_nicola", "hi": "hm_omega", "ja": "jm_kumo", "zh": "zm_yunjian"},
    "edge": {"en": "en-US-AndrewMultilingualNeural", "pt": "pt-BR-AntonioNeural", "pt-br": "pt-BR-AntonioNeural",
             "es": "es-MX-JorgeNeural", "fr": "fr-FR-HenriNeural", "de": "de-DE-ConradNeural",
             "it": "it-IT-DiegoNeural", "ja": "ja-JP-KeitaNeural", "zh": "zh-CN-YunxiNeural", "hi": "hi-IN-MadhurNeural"},
}

# RealtimeTTS sentence tokenizer language codes
TOKENIZER_LANG = {"pt-br": "pt", "zh": "zh", "ja": "ja"}


class Voice:
    def __init__(self, cfg: TTSConfig, lang: str, cache_dir: Path):
        self.cfg = cfg
        self.lang = lang.lower()
        self.cache_dir = cache_dir
        self.voice = cfg.voice or DEFAULT_VOICES.get(cfg.engine, {}).get(self.lang) or DEFAULT_VOICES.get(cfg.engine, {}).get(self.lang.split("-")[0])
        self._stream = None
        self._sig = json.dumps([cfg.engine, self.voice, cfg.speed, cfg.args, self.lang], sort_keys=True)

    # -- engine --------------------------------------------------------------
    def _build(self):
        rt = importlib.import_module("RealtimeTTS")
        cls_name = ENGINE_CLASSES.get(self.cfg.engine.lower(), self.cfg.engine)
        try:
            cls = getattr(rt, cls_name)
        except (AttributeError, ImportError) as e:
            extra = self.cfg.engine.lower()
            raise RuntimeError(
                f"TTS engine {cls_name!r} isn't available. Install it, e.g.  uv sync --extra {extra}  "
                f"(or: pip install \"realtimetts[{extra}]\")"
            ) from e
        kwargs = dict(self.cfg.args)
        engine_name = self.cfg.engine.lower()
        if engine_name == "kokoro":
            kwargs.setdefault("voice", self.voice or "am_michael")
            kwargs.setdefault("default_speed", self.cfg.speed)
        elif engine_name == "edge":
            kwargs.setdefault("rate", int(round((self.cfg.speed - 1.0) * 100)))
        elif engine_name in ("coqui", "xtts"):
            kwargs.setdefault("language", self.lang.split("-")[0])
            kwargs.setdefault("speed", self.cfg.speed)
        engine = cls(**kwargs)
        if self.voice and engine_name != "kokoro":
            try:
                engine.set_voice(self.voice)
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]could not set voice {self.voice!r}: {e}[/]")
        self._engine = engine
        self._direct = engine_name in DIRECT_ENGINES
        stream_cls = rt.TextToAudioStream
        self._stream = stream_cls(engine, muted=True, language=TOKENIZER_LANG.get(self.lang, self.lang.split("-")[0]))

    # -- synthesis -----------------------------------------------------------
    def path_for(self, text: str) -> Path:
        key = hashlib.sha1((self._sig + "\n" + text).encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{key}.wav"

    def synth(self, text: str) -> Path:
        dest = self.path_for(text)
        if dest.exists() and dest.stat().st_size > 1000:
            return dest
        if self._stream is None:
            self._build()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.wav")
        if self._direct and self._synth_direct(text, tmp):
            tmp.replace(dest)
            return dest
        self._stream.feed(text)
        self._stream.play(
            output_wavfile=str(tmp),
            muted=True,
            fast_sentence_fragment=False,
            comma_silence_duration=0.12,
            sentence_silence_duration=0.3,
            default_silence_duration=0.1,
            log_synthesized_text=False,
        )
        _ensure_wav(tmp)
        tmp.replace(dest)
        return dest


    def _synth_direct(self, text: str, dest: Path) -> bool:
        """Call the engine's synchronous synthesize() and drain its chunk queue.

        Skips TextToAudioStream's realtime playback machinery: ~5x realtime -> GPU speed."""
        import pyaudio

        eng = self._engine
        while not eng.queue.empty():
            eng.queue.get_nowait()
        try:
            ok = eng.synthesize(text)
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]direct synthesis failed ({e}); using the stream path[/]")
            self._direct = False
            return False
        chunks = []
        while not eng.queue.empty():
            chunks.append(eng.queue.get_nowait())
        if not ok or not chunks:
            return False
        fmt, channels, rate = eng.get_stream_info()
        data = b"".join(c if isinstance(c, bytes) else bytes(c) for c in chunks)
        if fmt == pyaudio.paInt16:
            audio = np.frombuffer(data, np.int16)
        elif fmt == pyaudio.paFloat32:
            audio = np.frombuffer(data, np.float32)
        else:  # compressed stream: let the stream path handle it
            self._direct = False
            return False
        sf.write(str(dest), audio.reshape(-1, max(1, channels)), rate)
        return True


# engines whose synthesize() is synchronous and emits raw PCM into engine.queue
DIRECT_ENGINES = {"kokoro", "coqui", "xtts"}


def _ensure_wav(path: Path) -> None:
    """Some engines hand back compressed audio; normalise to real WAV via ffmpeg."""
    if not path.exists() or path.stat().st_size < 100:
        raise RuntimeError(f"TTS produced no audio ({path.name})")
    try:
        sf.info(str(path))
    except Exception:  # noqa: BLE001
        fixed = path.with_suffix(".fix.wav")
        subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-y", "-i", str(path), str(fixed)], check=True)
        fixed.replace(path)


def load_audio(path: Path, sr: int) -> np.ndarray:
    data, file_sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if file_sr != sr:
        n = int(round(len(mono) * sr / file_sr))
        mono = np.interp(np.linspace(0, len(mono) - 1, n), np.arange(len(mono)), mono).astype(np.float32)
    return mono


# ------------------------------------------------------------ parallel voicing

_WORKER_VOICE: Voice | None = None


def _init_worker(cfg: TTSConfig, lang: str, cache_dir: str) -> None:
    global _WORKER_VOICE
    import warnings

    warnings.filterwarnings("ignore")
    _WORKER_VOICE = Voice(cfg, lang, Path(cache_dir))


def _synth_one(text: str) -> str:
    assert _WORKER_VOICE is not None
    return str(_WORKER_VOICE.synth(text))


def voice_all(cfg: TTSConfig, lang: str, cache_dir: Path, texts: list[str], progress=None) -> list[Path]:
    """Synthesize every text (cached ones are free). Local engines run in parallel processes."""
    voice = Voice(cfg, lang, cache_dir)
    todo = list(dict.fromkeys(t for t in texts if not (voice.path_for(t).exists() and voice.path_for(t).stat().st_size > 1000)))
    workers = cfg.workers if cfg.workers else (3 if cfg.engine.lower() in DIRECT_ENGINES else 1)
    workers = max(1, min(workers, len(todo)))
    if todo:
        if workers == 1:
            for i, t in enumerate(todo, 1):
                voice.synth(t)
                if progress:
                    progress(i, len(todo))
        else:
            from concurrent.futures import ProcessPoolExecutor

            # longest first so the pool drains evenly
            order = sorted(todo, key=len, reverse=True)
            with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(cfg, lang, str(cache_dir))) as pool:
                for i, _ in enumerate(pool.map(_synth_one, order, chunksize=2), 1):
                    if progress:
                        progress(i, len(todo))
    return [voice.path_for(t) for t in texts]
