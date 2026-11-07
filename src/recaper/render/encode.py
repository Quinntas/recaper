from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH")
    return path


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    try:
        r = subprocess.run(
            [ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
            timeout=30,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def video_codec_args(encoder: str, quality: int, fps: int) -> list[str]:
    use_nvenc = encoder == "nvenc" or (encoder == "auto" and nvenc_available())
    gop = ["-g", str(fps * 2)]
    if use_nvenc:
        # p3: ~2.4x the throughput of p5/hq on one NVENC engine; no visible difference on flat comic art.
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", str(quality), "-b:v", "0", *gop]  # p1: same quality as p3 at CQ 21 (PSNR 53 dB, SSIM .997), ~9% bigger, 1150 vs 870 fps
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality), *gop]


def open_raw_writer(out: Path, width: int, height: int, fps: int, codec_args: list[str]) -> subprocess.Popen:
    """Frames are written as YUV420 (I420), converted in-process: half the pipe bytes of BGR."""
    cmd = [
        ffmpeg_bin(), "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        *codec_args, "-pix_fmt", "yuv420p", "-an", str(out),
    ]  # fmt: skip
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


@lru_cache(maxsize=1)
def aac_mf_available() -> bool:
    """Windows' Media Foundation AAC encoder: ~3x the speed of ffmpeg's native one, same timing (no lag)."""
    try:
        r = subprocess.run(
            [ffmpeg_bin(), "-v", "error", "-f", "lavfi", "-i", "sine=d=0.3:r=48000", "-ac", "2", "-c:a", "aac_mf", "-b:a", "192k", "-f", "null", "-"],
            capture_output=True,
            timeout=30,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


MASTER_VERSION = 2  # bump when the mastering chain changes
SEGMENT_SECONDS = 1200.0  # long narrations are mastered in parallel pieces of about this length


def _afilter(loudness: float) -> str:
    return (
        "highpass=f=70,"
        "acompressor=threshold=-20dB:ratio=3:attack=5:release=90:makeup=2,"
        f"loudnorm=I={loudness}:TP=-1.5:LRA=9"
    )


def _aac_args() -> list[str]:
    # Stereo on purpose: ffmpeg's native AAC encoder can spin forever on mono @ 192k/48k.
    return ["-ac", "2", "-c:a", "aac_mf" if aac_mf_available() else "aac", "-b:a", "192k", "-ar", "48000"]


def quiet_cuts(narration: Path, every: float | None = None) -> list[int]:
    """Sample positions to split the narration at: the quietest 50 ms near every `every` seconds (the pauses
    between beats), so filters restarting on a new piece never touch speech."""
    import numpy as np
    import soundfile as sf

    every = every or SEGMENT_SECONDS
    search = min(30.0, every / 4)
    with sf.SoundFile(str(narration)) as f:
        sr, n = f.samplerate, f.frames
        cuts, win = [], max(1, int(sr * 0.05))
        t = every
        while (t + every / 2) * sr < n:
            a = max(0, int((t - search) * sr))
            f.seek(a)
            x = f.read(int(2 * search * sr), dtype="float32", always_2d=True).mean(axis=1)
            k = len(x) // win
            if k:
                energy = np.square(x[: k * win]).reshape(k, win).sum(axis=1)
                cuts.append(a + int(np.argmin(energy)) * win + win // 2)
            t += every
    return cuts


def master_audio(narration: Path, loudness: float, render_dir: Path, workers: int = 0) -> Path:
    """Pass 1: master the narration (filters + AAC). Independent of the video, so it runs while chunks render.
    Kept separate from the mux because recent ffmpeg builds deadlock when a filtergraph and a stream-less
    ffmetadata input share a command.

    The filters run at ~100x realtime on one core, so a 19 h book narration took ~12 min just to filter: long
    narrations are split at pauses, filtered in parallel to lossless FLAC, then encoded once (sample exact).
    The result is cached next to the chunks, keyed on the narration file and the settings."""
    import json
    import os
    from concurrent.futures import ThreadPoolExecutor

    import soundfile as sf

    master = render_dir / "narration_master.m4a"
    keyfile = render_dir / "narration_master.key"
    st = narration.stat()
    key = json.dumps([MASTER_VERSION, st.st_size, st.st_mtime_ns, loudness, _aac_args()])
    if master.exists() and keyfile.exists() and keyfile.read_text("utf-8") == key:
        return master
    keyfile.unlink(missing_ok=True)
    info = sf.info(str(narration))
    duration = info.frames / info.samplerate
    tmp = render_dir / "narration_master.tmp.m4a"
    afilter = _afilter(loudness)
    offset = pcm16_data_offset(narration) if duration > 1.5 * SEGMENT_SECONDS else None
    cuts = quiet_cuts(narration) if offset is not None else []
    if not cuts:
        _ffmpeg(["-i", str(narration), "-af", afilter, *_aac_args(), str(tmp)], "audio master", timeout=900 + duration / 10)
    else:
        bounds = list(zip([0, *cuts], [*cuts, info.frames]))
        pieces = [render_dir / f"narration_piece_{i:03d}.flac" for i in range(len(bounds))]

        def piece(i: int) -> None:
            a, b = bounds[i]
            _ffmpeg_from_pcm(narration, offset, info.samplerate, info.channels, a, b, ["-af", afilter, "-ar", str(info.samplerate), "-c:a", "flac", str(pieces[i])],
                             f"audio master piece {i + 1}/{len(bounds)}", timeout=600 + (b - a) / info.samplerate / 10)  # fmt: skip

        workers = workers or max(2, min(6, (os.cpu_count() or 4) // 4))  # the video chunks render at the same time
        try:
            with ThreadPoolExecutor(workers) as pool:
                list(pool.map(piece, range(len(bounds))))
            listing = render_dir / "narration_pieces.txt"
            listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in pieces), "utf-8")
            _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), *_aac_args(), str(tmp)], "audio master encode",
                    timeout=900 + duration / 20)  # fmt: skip
        finally:
            for p in pieces:
                p.unlink(missing_ok=True)
    tmp.replace(master)
    keyfile.write_text(key, "utf-8")
    return master


def pcm16_data_offset(path: Path) -> int | None:
    """Byte offset of the samples in a 16-bit PCM WAV / RF64 (what the voice stage writes), else None."""
    import struct

    import soundfile as sf

    if sf.info(str(path)).subtype != "PCM_16":
        return None
    with open(path, "rb") as f:
        head = f.read(1 << 16)
    if head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(head):
        cid, size = head[pos : pos + 4], struct.unpack("<I", head[pos + 4 : pos + 8])[0]
        if cid == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


def _ffmpeg_from_pcm(src: Path, offset: int, sr: int, channels: int, a: int, b: int, out_args: list[str], what: str,
                     timeout: float) -> None:  # fmt: skip
    """Feed samples [a, b) of a 16-bit PCM file to ffmpeg as raw PCM: sample-exact, unlike seeking by timestamp.
    Plain file reads, not libsndfile: its seeks failed ("psf_fseek() failed") with several pieces in flight."""
    import tempfile
    import time

    frame = 2 * channels
    with open(src, "rb") as f, tempfile.TemporaryFile() as err:
        cmd = [ffmpeg_bin(), "-v", "error", "-y", "-f", "s16le", "-ar", str(sr), "-ac", str(channels), "-i", "-", *out_args]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        deadline = time.time() + timeout
        try:
            f.seek(offset + a * frame)
            left = (b - a) * frame
            while left > 0:
                if time.time() > deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                block = f.read(min(left, sr * 30 * frame))
                if not block:
                    break
                try:
                    proc.stdin.write(block)
                except OSError:  # ffmpeg exited early: its stderr says why
                    break
                left -= len(block)
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait(timeout=max(1.0, deadline - time.time()))
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(f"{what} failed: {err.read().decode('utf-8', 'replace')[-1500:]}")


def mux_final(
    chunks: list[Path],
    narration: Path,
    out: Path,
    loudness: float,
    chapters_meta: Path | None,
    render_dir: Path,
    master: Path | None = None,
) -> None:
    listing = render_dir / "concat.txt"
    listing.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in chunks), "utf-8")
    master = master or master_audio(narration, loudness, render_dir)

    # Pass 2: stream-copy everything together (+ chapters).
    cmd = ["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(master)]
    if chapters_meta:
        cmd += ["-i", str(chapters_meta), "-map_chapters", "2"]
    cmd += ["-map", "0:v", "-map", "1:a", "-c", "copy", "-movflags", "+faststart", str(out)]
    import soundfile as sf

    # stream copy is disk-bound (+faststart writes the file twice): give very long videos room
    _ffmpeg(cmd, "final mux", timeout=900 + sf.info(str(narration)).duration / 10)


def _ffmpeg(args: list[str], what: str, timeout: float = 3600) -> None:
    r = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-y", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {r.stderr[-1500:]}")
