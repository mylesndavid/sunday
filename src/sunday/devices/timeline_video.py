"""Per-card timelapse encoding — bake a card's frames into one small H.264 MP4.

Why this exists: raw frames are pruned after a few days (rewind_macos retention +
size cap), but activity cards are meant to be a permanent record. So at synthesis
time we bake each card's frames into a compact MP4 stored ON the card. The scrubber
then plays that clip, and the card keeps its visual memory forever even after the
source frames are long gone.

H.264 is the right codec here: screen content (static regions, text) compresses
extremely well, so a multi-minute card lands around 100-300 KB — trivially small
for lifelong storage, and it plays natively in the Electron <video> tag.

ffmpeg comes from `imageio-ffmpeg` (a bundled static binary, so it works on any
user's Mac), falling back to a PATH ffmpeg when present (dev machines).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import structlog

log = structlog.get_logger("sunday.devices.timeline_video")

# Timelapse look: ~4 fps playback, downsample to roughly one frame per 20s of real
# time (capped) so a long card is a short watchable clip, scaled to 1280px wide.
PLAYBACK_FPS = 4
TARGET_SECONDS_PER_SAMPLE = 20.0
MAX_SAMPLES = 180
WIDTH = 1280
CRF = 28                # screen content is forgiving; 28 is small + still crisp
ENCODE_TIMEOUT = 90.0


def ffmpeg_exe() -> str | None:
    """Path to an ffmpeg binary — the bundled static one first (present on every
    user install), then a system ffmpeg (dev machines). None if neither is found."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:  # noqa: BLE001 — fall through to PATH
        pass
    import shutil
    return shutil.which("ffmpeg")


def sample_frames(frames: list[dict], target_gap_s: float = TARGET_SECONDS_PER_SAMPLE,
                  cap: int = MAX_SAMPLES) -> list[str]:
    """Pick a watchable subset of a card's frames: roughly one per `target_gap_s`
    of wall-clock, hard-capped at `cap`. `frames` is [{ts, image_path}] ascending.
    Returns existing image paths only (a missing file just drops out)."""
    usable = [f for f in frames if f.get("image_path") and os.path.exists(f["image_path"])]
    if not usable:
        return []
    picked: list[dict] = []
    last_ts = None
    for f in usable:
        if last_ts is None or (f["ts"] - last_ts) >= target_gap_s:
            picked.append(f)
            last_ts = f["ts"]
    if not picked or picked[-1] is not usable[-1]:
        picked.append(usable[-1])          # always include the final frame
    if len(picked) > cap:                   # thin evenly to the cap
        step = len(picked) / cap
        picked = [picked[int(i * step)] for i in range(cap)]
    return [f["image_path"] for f in picked]


async def encode_timelapse(image_paths: list[str], out_path: str,
                           fps: int = PLAYBACK_FPS, width: int = WIDTH,
                           crf: int = CRF, timeout: float = ENCODE_TIMEOUT) -> bool:
    """Encode `image_paths` (JPEGs, in order) into an H.264 MP4 at `out_path` via
    the concat demuxer. Returns True on a non-empty output file. Best-effort: any
    failure logs a warning and returns False (the card just keeps no clip)."""
    exe = ffmpeg_exe()
    if not exe:
        log.warning("timeline video: no ffmpeg available")
        return False
    paths = [p for p in image_paths if p and os.path.exists(p)]
    if not paths:
        return False

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    per = 1.0 / max(1, fps)
    # concat demuxer slideshow: each image shown `per` seconds; the last file must
    # be repeated because the demuxer ignores the final entry's duration.
    lines: list[str] = []
    for p in paths:
        safe = p.replace("'", "'\\''")
        lines.append(f"file '{safe}'")
        lines.append(f"duration {per:.4f}")
    lines.append(f"file '{paths[-1].replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")
    listfile = out.with_suffix(".concat.txt")
    try:
        listfile.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log.warning("timeline video: can't write concat list", error=str(exc))
        return False

    tmp = out.with_suffix(".tmp.mp4")
    cmd = [
        exe, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
        # even dims required by yuv420p; scale to width, keep aspect
        "-vf", f"scale={width}:-2,format=yuv420p",
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-movflags", "+faststart",
        str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("timeline video: encode timed out", out=str(out))
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("timeline video: encode failed to launch", error=str(exc))
        return False
    finally:
        listfile.unlink(missing_ok=True)

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        log.warning("timeline video: encode produced nothing",
                    rc=proc.returncode, err=(err or b"")[-300:].decode("utf-8", "replace"))
        tmp.unlink(missing_ok=True)
        return False
    try:
        tmp.replace(out)                    # atomic swap into place
    except OSError as exc:
        log.warning("timeline video: can't finalize", error=str(exc))
        tmp.unlink(missing_ok=True)
        return False
    return True
