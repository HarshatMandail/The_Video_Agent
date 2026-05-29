import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from .agent import run_agent1
from .browser_pool import shutdown_browser_pool
from .config import OUTPUT_DIR
from .models import Agent1Output
from .video_merger import merge_all_recordings, clean_old_clips

logger = logging.getLogger(__name__)

_VIDEO_PIPELINE_DIR = Path(__file__).resolve().parents[3] / "agent2_video_gen"


def _import_generate_tutorial():
    video_path_str = str(_VIDEO_PIPELINE_DIR)
    if video_path_str not in sys.path:
        sys.path.insert(0, video_path_str)
    from generate_tutorial import generate_tutorial_video
    return generate_tutorial_video


async def run_full_pipeline(
    url: str,
    user_query: str,
    cleanup_browser: bool = False,
) -> dict[str, Any]:
    """Execute the full pipeline: Agent 1 (record) → merge → Agent 2 (animate)."""
    logger.info("=" * 60)
    logger.info("FOXIO PIPELINE — Record → Merge → Split → Animate → Extend")
    logger.info(f"URL: {url}")
    logger.info(f"Query: {user_query}")
    logger.info("=" * 60)

    clean_old_clips()

    try:
        agent1_output: Agent1Output = await run_agent1(
            url=url,
            user_query=user_query,
            cleanup_browser=False,
        )
    except Exception as e:
        logger.error(f"Agent 1 failed: {e}")
        return {"status": "failed", "stage": "agent1", "error": str(e)}

    logger.info(
        f"Agent 1 complete | Platform: {agent1_output.platform_name} | "
        f"Pages: {len(agent1_output.pages_captured)}"
    )

    logger.info("Closing browser to finalize video recordings...")
    await shutdown_browser_pool()
    await asyncio.sleep(2)

    try:
        raw_video_path = merge_all_recordings(
            output_filename="raw_long_video.mp4",
            output_dir=OUTPUT_DIR,
            trim_start=agent1_output.trim_start_seconds,
        )
    except RuntimeError as e:
        logger.error(f"Video merge failed: {e}")
        return {
            "status": "partial",
            "stage": "merge_failed",
            "error": str(e),
            "agent1_output": agent1_output.model_dump(),
        }

    if not raw_video_path:
        logger.warning("No valid recordings to merge.")
        return {
            "status": "partial",
            "stage": "no_recordings",
            "error": "No valid video recordings found after browser task.",
            "agent1_output": agent1_output.model_dump(),
        }

    logger.info(f"Merged raw video: {raw_video_path}")

    try:
        generate_tutorial_video = _import_generate_tutorial()
    except (ImportError, ModuleNotFoundError) as e:
        logger.error(f"Cannot import video pipeline: {e}")
        return {
            "status": "partial",
            "stage": "import_failed",
            "error": str(e),
            "agent1_output": agent1_output.model_dump(),
            "raw_video_path": raw_video_path,
        }

    user_prompt = (
        f"Professional SaaS tutorial for {agent1_output.platform_name}. "
        f"Smooth realistic cursor, clean 60fps motion, ultra sharp. "
        f"Task: {user_query}\n\n"
        f"NARRATION SCRIPT (follow this exactly for voice-over and cursor movements):\n"
        f"{agent1_output.context_for_video}"
    )

    try:
        video_result = await generate_tutorial_video(
            raw_video_path=raw_video_path,
            user_prompt=user_prompt,
            platform_name=agent1_output.platform_name,
        )
    except Exception as e:
        logger.error(f"Video generation failed: {e}")
        return {
            "status": "partial",
            "stage": "video_failed",
            "error": str(e),
            "agent1_output": agent1_output.model_dump(),
            "raw_video_path": raw_video_path,
        }

    logger.info(
        f"Video generation complete | Status: {video_result.get('status')} | "
        f"Video: {video_result.get('final_video_path', 'N/A')}"
    )

    logger.info("=" * 60)
    logger.info("FULL PIPELINE COMPLETE")
    logger.info("=" * 60)

    return {
        "status": "completed",
        "stage": "done",
        "agent1_output": agent1_output.model_dump(),
        "raw_video_path": raw_video_path,
        "video_result": video_result,
    }
