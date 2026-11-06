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


def master_audio(narration: Path, loudness: float, render_dir: Path) -> Path:
    """Pass 1: master the narration (filters + AAC). Independent of the video, so it runs while chunks render.
    Kept separate from the mux because recent ffmpeg builds deadlock when a filtergraph and a stream-less
    ffmetadata input share a command."""
    master = render_dir / "narration_master.m4a"
    afilter = (
        "highpass=f=70,"
        "acompressor=threshold=-20dB:ratio=3:attack=5:release=90:makeup=2,"
        f"loudnorm=I={loudness}:TP=-1.5:LRA=9"
    )
    # Stereo on purpose: ffmpeg's native AAC encoder can spin forever on mono @ 192k/48k.
    _ffmpeg(
        ["-i", str(narration), "-af", afilter, "-ac", "2", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(master)],
        "audio master",
        timeout=900,
    )
    return master


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
    _ffmpeg(cmd, "final mux", timeout=900)


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
