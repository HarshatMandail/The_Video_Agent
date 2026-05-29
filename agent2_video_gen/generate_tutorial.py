"""
Tutorial Video Generator — Takes a single merged raw video and produces a polished tutorial.

Usage:
    from generate_tutorial import generate_tutorial_video

    result = await generate_tutorial_video(
        raw_video_path="/path/to/raw_recording.mp4",
        user_prompt="Smooth cursor, professional SaaS tutorial",
        platform_name="Salesforce",
    )
    print(result["final_video_path"])
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from config.settings import settings
from graph.workflow import run_pipeline


async def generate_tutorial_video(
    raw_video_path: str,
    user_prompt: str = "",
    platform_name: str = "Salesforce",
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    Generate a polished tutorial video from a single raw browser recording.

    Flow:
      1. Split raw video into ≤8.0s clips
      2. Process sequentially: clip 0 = animate, clips 1+ = extend-video
      3. Concatenate enhanced clips into final smooth video
      4. Save metadata

    Args:
        raw_video_path: Path to the merged raw .mp4 from Agent 1.
        user_prompt: Enhancement prompt (cursor smoothing, style, etc.).
        platform_name: Platform name for prompt context.
        job_id: Optional custom job ID.

    Returns:
        Dict with status, final_video_path, metadata_path, error.
    """
    resolved_job_id = job_id or str(uuid.uuid4())[:8]

    logger.info("=" * 60)
    logger.info(f"[Job {resolved_job_id}] TUTORIAL VIDEO GENERATION")
    logger.info(f"Platform: {platform_name}")
    logger.info(f"Raw video: {raw_video_path}")
    logger.info("=" * 60)

    if not Path(raw_video_path).exists():
        return _error_result(resolved_job_id, f"Raw video not found: {raw_video_path}")

    # Default prompt if none provided
    if not user_prompt:
        user_prompt = (
            "Make mouse cursor smooth and realistic. "
            "Professional clean SaaS tutorial style, ultra sharp, 60fps."
        )

    # Run the pipeline
    try:
        pipeline_result = await run_pipeline(
            raw_video_path=raw_video_path,
            user_prompt=user_prompt,
            platform_name=platform_name,
            job_id=resolved_job_id,
        )
    except Exception as e:
        logger.error(f"[Job {resolved_job_id}] Pipeline failed: {e}")
        return _error_result(resolved_job_id, f"Pipeline failed: {e}")

    final_status = pipeline_result.get("status", "unknown")
    final_video_path = pipeline_result.get("final_video_path", "")

    # Save metadata
    metadata_path = _save_metadata(
        job_id=resolved_job_id,
        raw_video_path=raw_video_path,
        platform_name=platform_name,
        pipeline_result=pipeline_result,
    )

    logger.info("=" * 60)
    logger.info(f"[Job {resolved_job_id}] COMPLETE | status={final_status}")
    logger.info(f"[Job {resolved_job_id}] Video: {final_video_path}")
    logger.info("=" * 60)

    return {
        "status": final_status,
        "job_id": resolved_job_id,
        "final_video_path": final_video_path,
        "metadata_path": str(metadata_path) if metadata_path else "",
        "clips_processed": len(pipeline_result.get("clip_results", [])),
        "clip_results": pipeline_result.get("clip_results", []),
        "error": pipeline_result.get("error", ""),
    }


def _save_metadata(job_id: str, raw_video_path: str, platform_name: str, pipeline_result: dict) -> Path | None:
    """Save run metadata as JSON."""
    try:
        settings.final_output_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = settings.final_output_dir / f"{job_id}_metadata.json"

        metadata = {
            "job_id": job_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_video_path": raw_video_path,
            "platform_name": platform_name,
            "pipeline_status": pipeline_result.get("status"),
            "final_video_path": pipeline_result.get("final_video_path"),
            "clips_count": len(pipeline_result.get("clips", [])),
            "clip_results": pipeline_result.get("clip_results", []),
        }

        metadata_path.write_text(json.dumps(metadata, indent=2, default=str))
        return metadata_path
    except Exception as e:
        logger.warning(f"[Job {job_id}] Failed to save metadata: {e}")
        return None


def _error_result(job_id: str, error: str) -> dict[str, Any]:
    """Standardized error result."""
    return {
        "status": "failed",
        "job_id": job_id,
        "final_video_path": "",
        "metadata_path": "",
        "clips_processed": 0,
        "clip_results": [],
        "error": error,
    }
