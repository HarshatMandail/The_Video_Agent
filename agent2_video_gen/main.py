"""
Video Generation Pipeline — Main Entry Point

Demonstrates the Split → Animate → Extend-Video pipeline.
In production, raw_video_path comes from Agent 1's merged recording.

Usage:
    python main.py                    # Run with a sample video (dry-run)
    python main.py /path/to/video.mp4 # Run with a real video
"""

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger

from config.settings import settings
from generate_tutorial import generate_tutorial_video
from nodes.cursor_overlay import apply_cursor_overlay

logger.remove()
logger.add(
    sys.stdout,
    level=settings.log_level,
    format=(
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
        "<level>{message}</level>"
    ),
)


def _create_sample_video() -> str:
    """Create a 20-second sample video to demonstrate splitting into 3 clips."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="sample_raw_"))
    output = tmp_dir / "sample_raw_recording.mp4"

    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=0x1a1a2e:s=1280x720:d=20",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "20",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            str(output),
        ]
        subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        logger.info(f"Sample 20s video created: {output}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        output.write_bytes(b"\x00" * 1024)
        logger.warning("FFmpeg unavailable — created dummy file for dry-run")

    return str(output)


async def main() -> None:
    """Run the pipeline."""
    # ─── Cursor Overlay Mode (independent of Grok) ───────────────────────────
    if "--cursor-overlay" in sys.argv:
        _run_cursor_overlay()
        return

    # ─── Standard Grok Pipeline ──────────────────────────────────────────────
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        raw_video_path = sys.argv[1]
        if not Path(raw_video_path).exists():
            logger.error(f"File not found: {raw_video_path}")
            return
    else:
        raw_video_path = _create_sample_video()

    logger.info("=" * 60)
    logger.info("VIDEO PIPELINE — Split → Animate → Extend")
    logger.info(f"Input: {raw_video_path}")
    logger.info(f"Dry Run: {settings.dry_run}")
    logger.info("=" * 60)

    result = await generate_tutorial_video(
        raw_video_path=raw_video_path,
        user_prompt="Smooth realistic cursor, professional SaaS tutorial, 60fps.",
        platform_name="Salesforce",
    )

    logger.info("=" * 60)
    logger.info("RESULT")
    logger.info("=" * 60)
    logger.info(f"Status: {result['status']}")
    logger.info(f"Job ID: {result['job_id']}")
    logger.info(f"Clips processed: {result['clips_processed']}")

    final_path = result.get("final_video_path", "")
    if final_path and final_path != "dry_run_no_output":
        logger.success(f"Final video: {final_path}")
    elif final_path == "dry_run_no_output":
        logger.info("Video: dry_run (no file generated)")

    if result.get("error"):
        logger.error(f"Error: {result['error']}")


def _run_cursor_overlay() -> None:
    """
    Run FFmpeg cursor overlay independently.

    Usage:
        python main.py --cursor-overlay <video_path> <cursor_actions.json> [--no-ripple]
    """
    args = [a for a in sys.argv[1:] if a != "--cursor-overlay"]
    no_ripple = "--no-ripple" in args
    args = [a for a in args if a != "--no-ripple"]

    if len(args) < 2:
        print("Usage: python main.py --cursor-overlay <video.mp4> <cursor_actions.json> [--no-ripple]")
        print("\nExample:")
        print("  python main.py --cursor-overlay ../output/raw_video.mp4 ../output/cursor_actions.json")
        return

    video_path, actions_path = args[0], args[1]

    if not Path(video_path).exists():
        logger.error(f"Video not found: {video_path}")
        return
    if not Path(actions_path).exists():
        logger.error(f"cursor_actions.json not found: {actions_path}")
        return

    logger.info("=" * 60)
    logger.info("CURSOR OVERLAY — FFmpeg Programmatic Mode")
    logger.info(f"Video: {video_path}")
    logger.info(f"Actions: {actions_path}")
    logger.info(f"Ripple: {'enabled' if not no_ripple else 'disabled'}")
    logger.info("=" * 60)

    try:
        output = apply_cursor_overlay(
            video_path=video_path,
            cursor_actions_path=actions_path,
            show_ripple=not no_ripple,
        )
        logger.success(f"Done! Output: {output}")
    except Exception as e:
        logger.error(f"Cursor overlay failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
