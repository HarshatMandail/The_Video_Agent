"""
Utility functions for video concatenation (FFmpeg), preprocessing, and file management.
"""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

from loguru import logger

from config.settings import settings


def ensure_directories() -> None:
    """Create required output directories if they don't exist."""
    settings.clip_output_dir.mkdir(parents=True, exist_ok=True)
    settings.final_output_dir.mkdir(parents=True, exist_ok=True)


def cleanup_clips() -> None:
    """Remove all temporary clip files after concatenation."""
    if settings.clip_output_dir.exists():
        shutil.rmtree(settings.clip_output_dir)
        logger.info(f"Cleaned up clips directory: {settings.clip_output_dir}")


def cleanup_preprocessed() -> None:
    """Remove all temporary preprocessed .mp4 files after final video is produced."""
    if not settings.cleanup_temp_files:
        logger.info("[Cleanup] Skipping preprocessed cleanup (cleanup_temp_files=False)")
        return

    if settings.preprocess_output_dir.exists():
        file_count = len(list(settings.preprocess_output_dir.glob("*.mp4")))
        shutil.rmtree(settings.preprocess_output_dir)
        logger.info(
            f"[Cleanup] Removed {file_count} preprocessed files: "
            f"{settings.preprocess_output_dir}"
        )


def concatenate_clips(clip_paths: list[str], job_id: str) -> Path:
    """
    Concatenate multiple video clips with 1-second cross-fade transitions.

    Uses FFmpeg filter_complex with xfade between consecutive clips.
    The 1s cross-fade blends the overlapping regions for seamless transitions.
    """
    if not clip_paths:
        raise ValueError("No clips to concatenate")

    settings.final_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = settings.final_output_dir / f"tutorial_{job_id}.mp4"

    sorted_paths = sorted(clip_paths)
    n = len(sorted_paths)

    print(f"\n🔄 Concatenating {n} clips...")
    logger.info(f"[Concat] Starting concatenation of {n} clips into: {output_path}")

    # Single clip — no cross-fade needed
    if n == 1:
        logger.info("[Concat] Single clip — copying directly")
        cmd = [
            "ffmpeg", "-y",
            "-i", sorted_paths[0],
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
            final_dur = probe_duration(output_path) or 0
            print(f"✅ Final video: {output_path.name} ({final_dur:.1f}s)")
            logger.success(f"[Concat] Final video created: {output_path} ({final_dur:.1f}s)")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"FFmpeg failed: {e.stderr[:200]}") from e
        return output_path

    # Probe durations of each clip
    xfade_duration = 1.0
    clip_durations = []
    for i, p in enumerate(sorted_paths):
        dur = probe_duration(Path(p)) or 8.0
        clip_durations.append(dur)
        logger.info(f"[Concat] Clip {i}: {Path(p).name} — {dur:.2f}s")

    total_input = sum(clip_durations)
    expected_output = total_input - (xfade_duration * (n - 1))
    print(f"   Input total: {total_input:.1f}s | Cross-fades: {n-1} x {xfade_duration}s | Expected output: {expected_output:.1f}s")
    logger.info(f"[Concat] Total input: {total_input:.1f}s | Expected output: {expected_output:.1f}s")

    # Build FFmpeg command with inputs
    cmd = ["ffmpeg", "-y"]
    for p in sorted_paths:
        cmd.extend(["-i", str(Path(p).resolve())])

    # Build xfade filter chain with correct cumulative offsets
    # Offset for xfade[i] = (sum of durations 0..i) - (i * xfade_duration) - xfade_duration
    # Simplified: offset[i] = sum(durations[0:i+1]) - (i+1) * xfade_duration
    filter_parts = []

    for i in range(n - 1):
        # Calculate offset: point in the OUTPUT timeline where this xfade starts
        offset = sum(clip_durations[:i + 1]) - (i + 1) * xfade_duration
        offset = max(0, offset)  # safety

        if i == 0:
            in1 = "[0:v]"
            in2 = "[1:v]"
        else:
            in1 = f"[v{i}]"
            in2 = f"[{i + 1}:v]"

        out_label = "[outv]" if i == n - 2 else f"[v{i + 1}]"

        filter_parts.append(
            f"{in1}{in2}xfade=transition=fade:duration={xfade_duration}:offset={offset:.3f}{out_label}"
        )
        logger.info(f"[Concat] xfade {i}: offset={offset:.3f}s {in1}+{in2} -> {out_label}")

    filter_complex = ";".join(filter_parts)

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-r", "30",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
        final_dur = probe_duration(output_path) or 0
        print(f"✅ Final video: {output_path.name} ({final_dur:.1f}s)")
        logger.success(f"[Concat] Final video with cross-fade: {output_path} ({final_dur:.1f}s)")
    except subprocess.CalledProcessError as e:
        logger.error(f"[Concat] FFmpeg xfade failed: {e.stderr[:500]}")
        print(f"⚠️  Cross-fade failed, falling back to simple concat...")
        logger.warning("[Concat] Falling back to simple concat without cross-fade...")
        return _fallback_concat(sorted_paths, output_path)
    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found. Install FFmpeg and ensure it's in PATH.")

    return output_path


def _fallback_concat(clip_paths: list[str], output_path: Path) -> Path:
    """Simple concat fallback if xfade fails."""
    concat_list_path = settings.clip_output_dir / "concat_list.txt"
    with open(concat_list_path, "w") as f:
        for clip_path in clip_paths:
            f.write(f"file '{Path(clip_path).resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list_path),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        logger.success(f"Fallback concat created: {output_path}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Fallback concat failed: {e.stderr[:200]}") from e
    finally:
        concat_list_path.unlink(missing_ok=True)

    return output_path


def preprocess_video_for_grok(video_path: str) -> str:
    """
    Preprocess a raw Playwright .webm recording for Grok Imagine Video edit-video mode.

    Steps:
      1. Convert .webm → .mp4 (H.264 video + AAC audio)
      2. Trim to max duration (keeps the last N seconds where the action happens)
      3. Normalize FPS to configured value (30 or 60)
      4. Scale to configured resolution (1280x720 or 1920x1080)

    Args:
        video_path: Path to the raw .webm (or any video) from Playwright.

    Returns:
        Path to the processed .mp4 file ready for upload to xAI.

    Raises:
        RuntimeError: If FFmpeg fails or input file doesn't exist.
    """
    input_path = Path(video_path)
    if not input_path.exists():
        raise RuntimeError(f"[Preprocess] Input video not found: {video_path}")

    settings.preprocess_output_dir.mkdir(parents=True, exist_ok=True)

    # Generate unique output filename
    clip_id = uuid.uuid4().hex[:8]
    output_path = settings.preprocess_output_dir / f"{input_path.stem}_{clip_id}.mp4"

    max_duration = settings.preprocess_max_duration
    fps = settings.preprocess_fps
    width = settings.preprocess_width
    height = settings.preprocess_height

    # Probe input duration to decide trim strategy
    input_duration = probe_duration(input_path)

    # Build FFmpeg command
    cmd = ["ffmpeg", "-y"]

    # If input is longer than max, seek to keep the LAST N seconds (where action occurs)
    if input_duration and input_duration > max_duration:
        seek_to = input_duration - max_duration
        cmd.extend(["-ss", f"{seek_to:.2f}"])
        logger.info(
            f"[Preprocess] Trimming: {input_duration:.1f}s → last {max_duration}s "
            f"(seeking to {seek_to:.1f}s)"
        )

    cmd.extend(["-i", str(input_path)])

    # Duration limit (hard cap)
    cmd.extend(["-t", str(max_duration)])

    # Video: H.264, target FPS, scaled resolution, pixel format for compatibility
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-r", str(fps),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
        "-pix_fmt", "yuv420p",
    ])

    # Audio: AAC (or silent if no audio stream)
    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2"])

    # Fast-start for streaming compatibility
    cmd.extend(["-movflags", "+faststart"])

    cmd.append(str(output_path))

    logger.info(
        f"[Preprocess] Converting: {input_path.name} → {output_path.name} | "
        f"fps={fps} | res={width}x{height} | max_dur={max_duration}s"
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        logger.success(f"[Preprocess] Done: {output_path} ({output_path.stat().st_size // 1024}KB)")
    except subprocess.CalledProcessError as e:
        logger.error(f"[Preprocess] FFmpeg failed: {e.stderr[:500]}")
        raise RuntimeError(f"Video preprocessing failed: {e.stderr[:200]}") from e
    except FileNotFoundError:
        raise RuntimeError(
            "[Preprocess] FFmpeg not found. Install FFmpeg and ensure it's in PATH."
        )

    return str(output_path)


def probe_duration(video_path: Path) -> float | None:
    """
    Probe video duration using FFprobe.

    Returns duration in seconds, or None if probing fails.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(video_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )

        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration", 0))
        return duration if duration > 0 else None

    except Exception as e:
        logger.warning(f"[Preprocess] FFprobe failed, skipping trim optimization: {e}")
        return None
