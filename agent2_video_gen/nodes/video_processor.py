"""
Video Processor — Sequential clip enhancement using edit-video mode.

Single prompt source of truth for the entire pipeline.
Each clip is processed independently against its own raw source.

Supports two modes:
  - Clip mode (default): Process split clips with ENHANCEMENT_PROMPT
  - Video edit mode: Process full raw video with GROK_IMAGINE_VIDEO_EDIT_PROMPT + cursor metadata
"""

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from adapters import get_adapter
from config.settings import settings
from nodes.video_splitter import build_video_edit_prompt

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
NON_RETRYABLE_KEYWORDS = {"moderation", "invalid_argument", "permission_denied", "invalid"}

# ─── Single Source of Truth: The Grok API Prompt ──────────────────────────────

ENHANCEMENT_PROMPT = """You are enhancing a raw screen recording of the Salesforce Lightning UI.
Your ONLY job is to make the mouse cursor slightly smoother and more visible while keeping the video 100% identical to the source.

STRICT RULES - YOU MUST OBEY ALL OF THEM:
- Preserve EVERY pixel, layout, text, button, icon, color, font, and data EXACTLY as in the source.
- Do NOT add, remove, move, or change ANY UI element.
- Do NOT add any logos, watermarks, arrows, highlights, animations, or overlays.
- Do NOT change the visual style — keep it as a clean, realistic screen recording.
- Do NOT add any creative elements, cartoon characters, or tutorial effects.
- Do NOT shorten or lengthen the video — output must be exactly the same duration as the input clip.
- Mouse cursor must be small, standard arrow pointer, same size in every frame, slightly smoother movement only.
- No voice-over, no sound, no text captions.

Output must look like the original screen recording but with a slightly polished cursor. Nothing else."""


async def process_clips_sequentially(
    clips: list[dict],
    user_prompt: str,
    platform_name: str = "Salesforce",
    use_video_edit_mode: bool = False,
    input_dir: Path | None = None,
) -> list[dict[str, Any]]:
    # PIPELINE MODE: Independent edit-video per raw clip + overlap + cross-fade
    # Cursor is forced to small consistent default size in every clip
    """
    Process all clips using edit-video mode independently.

    Args:
        clips: List of clip dicts from video_splitter (index, path, duration).
        user_prompt: Additional user context (unused in clip mode, prompt is fixed).
        platform_name: Platform name for prompt context.
        use_video_edit_mode: If True, uses GROK_IMAGINE_VIDEO_EDIT_PROMPT with
            cursor metadata instead of the default ENHANCEMENT_PROMPT.
        input_dir: Directory containing cursor_actions.json (required for video edit mode).

    Returns:
        List of result dicts with status, path, mode for each clip.
    """
    adapter = get_adapter()
    results: list[dict[str, Any]] = []
    total = len(clips)

    # Select prompt based on mode
    if use_video_edit_mode and input_dir:
        prompt = build_video_edit_prompt(input_dir)
        logger.info(f"[Processor] Using VIDEO EDIT MODE with cursor metadata from {input_dir}")
    else:
        prompt = ENHANCEMENT_PROMPT
        if use_video_edit_mode and not input_dir:
            logger.warning(
                "[Processor] use_video_edit_mode=True but no input_dir provided. "
                "Falling back to standard enhancement prompt."
            )

    logger.info(f"[Processor] Processing {total} clips (edit-video mode)...")

    for clip in clips:
        clip_path = clip["path"]
        clip_duration = max(1, min(int(round(clip["duration"])), 8))  # xAI API limit: 1-8s
        clip_index = clip["index"]

        output_path = settings.clip_output_dir / f"enhanced_{clip_index:03d}.mp4"

        result = await _process_with_retry(
            adapter=adapter,
            input_video_path=clip_path,
            prompt=prompt,
            duration=clip_duration,
            output_path=output_path,
            clip_index=clip_index,
        )

        results.append(result)

        status_icon = "\u2713" if result["status"] in ("success", "dry_run") else "\u2717"
        logger.info(f"[Processor] Clip {clip_index}/{total - 1} {status_icon}")

    successful = sum(1 for r in results if r["status"] in ("success", "dry_run"))
    logger.info(f"[Processor] Done | {successful}/{total} successful")

    return results


async def _process_with_retry(
    adapter,
    input_video_path: str,
    prompt: str,
    duration: float,
    output_path: Path,
    clip_index: int,
) -> dict[str, Any]:
    """Process a single clip with exponential backoff retry."""
    last_error = ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = await adapter.generate_video(
                input_video_path=input_video_path,
                prompt=prompt,
                duration=duration,
                output_path=output_path,
            )

            return {
                "clip_index": clip_index,
                "status": result.get("status", "success"),
                "path": result.get("path", str(output_path)),
                "mode": "edit-video",
                "cost_usd": result.get("cost_usd"),
            }

        except Exception as e:
            last_error = str(e)

            if any(kw in last_error.lower() for kw in NON_RETRYABLE_KEYWORDS):
                logger.error(f"[Processor] Clip {clip_index} non-retryable: {last_error}")
                break

            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    f"[Processor] Clip {clip_index} attempt {attempt}/{MAX_RETRIES} "
                    f"failed: {last_error}. Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)

    logger.error(f"[Processor] Clip {clip_index} failed: {last_error}")
    return {
        "clip_index": clip_index,
        "status": "failed",
        "path": input_video_path,
        "mode": "edit-video",
        "error": last_error,
    }
