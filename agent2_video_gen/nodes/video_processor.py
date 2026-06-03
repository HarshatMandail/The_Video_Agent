"""
Video Processor — Splits raw video into clips, processes each with Grok Imagine Video,
and provides the single source-of-truth prompt for the entire pipeline.

Uses cursor_actions.json from Agent 1 for precise cursor overlay with click animations.

Flow:
  1. Split raw video into ≤8.0s clips (Grok API limit)
  2. Process each clip with GROK_VIDEO_PROMPT + cursor metadata
  3. Return enhanced clips for concatenation
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from adapters import get_adapter
from config.settings import settings
from nodes.utils import probe_duration

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
NON_RETRYABLE_KEYWORDS = {"moderation", "invalid_argument", "permission_denied", "invalid"}
CREDIT_EXHAUSTED_KEYWORDS = {"credits", "spending limit", "permission_denied"}


class CreditExhaustedError(Exception):
    """Raised when xAI API credits are exhausted — triggers early termination."""
    pass

CLIP_DURATION = 8.0
OVERLAP_SECONDS = 1.0
STEP_SECONDS = CLIP_DURATION - OVERLAP_SECONDS


# ═══════════════════════════════════════════════════════════════════════════════
# THE PROMPT — Single source of truth. Edit THIS to change video output quality.
# ═══════════════════════════════════════════════════════════════════════════════

GROK_VIDEO_PROMPT = """\
ULTRA STRICT SCREEN RECORDING EDIT MODE — PIXEL PERFECT PRESERVATION REQUIRED

You are performing precise editing on a raw Salesforce screen recording.
Your ONLY task is to add a clean mouse cursor and minimal click feedback.
The original video must remain 100% unchanged in every single frame.

## NON-NEGOTIABLE RULES

1. PIXEL-LEVEL PRESERVATION (MOST IMPORTANT)
   - Keep EVERY pixel of the original video EXACTLY as it appears in the source.
   - Do NOT change brightness, contrast, saturation, hue, or color of ANY UI element.
   - Do NOT apply any color grading, film look, haze, blur, or sharpening to the background/UI.
   - **Text must remain perfectly sharp, clear, and 100% readable exactly like the original source video.** Never make text blurry, low-contrast, faded, or altered in any way.

2. CURSOR RULES
   - Use ONLY a clean, standard white arrow cursor with thin dark outline (realistic desktop style).
   - Cursor size must be consistent (~20px) across all frames.
   - Movement should be smooth but natural. Do NOT stylize or exaggerate movement.
   - Never add glow, shadow, or extra effects to the cursor itself.

3. CLICK FEEDBACK (MINIMAL)
   - On click actions, show a very subtle, small circular ripple (max 20px radius).
   - Ripple must be short (150-180ms) and low opacity.
   - Use soft blue: rgba(66, 133, 244, 0.5)
   - Do NOT make the ripple flashy or large.

4. CURSOR ACTIONS
{cursor_actions_block}

## ZERO TOLERANCE RULES
- Do NOT re-render, regenerate, or artistically reinterpret any part of the UI.
- Do NOT zoom, pan, crop, or change resolution.
- Do NOT add any highlights, arrows, labels, tooltips, or extra elements.
- Do NOT change any text, even slightly.
- Do NOT apply cinematic effects, transitions, or creative styling.
- Follow cursor_actions.json timestamps exactly.

## NEGATIVE PROMPT (AVOID AT ALL COSTS)
color shift, color grading, brightness change, washed out, hazy, blurry text, soft text, low contrast text, faded text, unreadable text, glowing cursor, big cursor, cartoon cursor, extra glow, shadow on UI, cinematic look, film grain, artistic effect, added elements, logo, watermark, distorted UI, changed layout, hallucinated buttons, zoom, pan, creative cursor movement

## OUTPUT STYLE
Clean, sharp, realistic enterprise screen recording. Text must be perfectly clear and readable like the original. Natural cursor movement. Minimal and professional click feedback only. Zero AI artifacts. Looks like a high-quality raw screen recording with cursor overlay.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# CURSOR ACTIONS LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def load_cursor_actions(input_dir: Path) -> Optional[list[dict]]:
    """Load cursor_actions.json from the input directory."""
    cursor_file = input_dir / "cursor_actions.json"
    if not cursor_file.exists():
        logger.info("[Processor] No cursor_actions.json found — cursor will use fallback positioning.")
        return None

    try:
        raw_text = cursor_file.read_text(encoding="utf-8").strip()
        if not raw_text:
            return None

        data = json.loads(raw_text)
        if not isinstance(data, list):
            return None

        valid_actions = [
            a for a in data
            if isinstance(a, dict) and "timestamp" in a and "action" in a
        ]

        if not valid_actions:
            return None

        logger.info(f"[Processor] Loaded {len(valid_actions)} cursor actions")
        return valid_actions

    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[Processor] Cannot read cursor_actions.json: {e}")
        return None


def build_prompt(input_dir: Path) -> str:
    """Build the full Grok prompt, injecting cursor timing from cursor_actions.json."""
    actions = load_cursor_actions(input_dir)

    if actions:
        lines = []
        for action in actions:
            ts = action.get("timestamp", 0)
            act = action.get("action", "unknown")
            x = action.get("x", 0)
            y = action.get("y", 0)
            element = action.get("element", "").strip()
            desc = action.get("description", "").strip()

            if act == "click":
                target_label = f' on "{element}"' if element else ""
                note = f" — {desc}" if desc else ""
                lines.append(f"  - {ts:.2f}s: CLICK at ({x}, {y}){target_label}{note}")
            elif act == "hover":
                lines.append(f"  - {ts:.2f}s: MOVE to ({x}, {y})")
            elif act == "keypress":
                key = action.get("key", "")
                context = f' in "{element}"' if element else ""
                lines.append(f"  - {ts:.2f}s: KEYPRESS '{key}'{context}")
            elif act == "scroll":
                delta_y = action.get("deltaY", 0)
                direction = "down" if delta_y > 0 else "up"
                lines.append(f"  - {ts:.2f}s: SCROLL {direction} at ({x}, {y})")

        cursor_block = (
            "The following cursor actions were recorded during the browser session.\n"
            "Map each timestamp proportionally to the output video duration.\n"
            "Move the cursor smoothly between consecutive positions using bezier easing.\n\n"
            + "\n".join(lines)
        )
    else:
        cursor_block = (
            "No cursor action data is available for this recording.\n"
            "Render the cursor stationary at the viewport center (640, 360) for the\n"
            "entire video duration. Do not animate movement."
        )

    return GROK_VIDEO_PROMPT.format(cursor_actions_block=cursor_block)


# ═══════════════════════════════════════════════════════════════════════════════
# VIDEO SPLITTING
# ═══════════════════════════════════════════════════════════════════════════════

def split_video_into_clips(
    raw_video_path: str,
    output_dir: Path | None = None,
    max_seconds: float = CLIP_DURATION,
) -> list[dict]:
    """Split a raw .mp4 into overlapping ≤8s clips for Grok API."""
    input_path = Path(raw_video_path)
    if not input_path.exists():
        raise RuntimeError(f"[Processor] Input video not found: {raw_video_path}")

    out_dir = output_dir or settings.clip_output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    total_duration = probe_duration(input_path)
    if not total_duration or total_duration <= 0:
        raise RuntimeError(f"[Processor] Cannot determine video duration: {raw_video_path}")

    clip_starts = _calculate_clip_starts(total_duration)

    logger.info(
        f"[Processor] Splitting {input_path.name} ({total_duration:.1f}s) "
        f"into {len(clip_starts)} clips of ≤{max_seconds}s with {OVERLAP_SECONDS}s overlap"
    )

    clips = []
    for i, start in enumerate(clip_starts):
        remaining = total_duration - start
        duration = min(max_seconds, remaining)

        if duration < 1.0:
            break

        output_file = out_dir / f"split_{i:03d}.mp4"
        _extract_clip(input_path, output_file, start, duration, i)

        actual_duration = probe_duration(output_file) or duration

        clips.append({
            "index": i,
            "path": str(output_file),
            "start_time": start,
            "duration": actual_duration,
        })

    logger.info(f"[Processor] Created {len(clips)} clips from {total_duration:.1f}s source")
    return clips


def _calculate_clip_starts(total_duration: float) -> list[float]:
    starts = []
    t = 0.0
    while t < total_duration:
        starts.append(t)
        t += STEP_SECONDS
        if t >= total_duration and (total_duration - starts[-1]) < 1.0:
            break
    return starts


def _extract_clip(input_path: Path, output_file: Path, start: float, duration: float, index: int) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-r", "30",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(output_file),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Splitting failed at clip {index}: {e.stderr[:200]}") from e
    except FileNotFoundError:
        raise RuntimeError("[Processor] FFmpeg not found. Install FFmpeg.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLIP PROCESSING (Grok API calls)
# ═══════════════════════════════════════════════════════════════════════════════

async def process_clips_sequentially(
    clips: list[dict],
    user_prompt: str,
    platform_name: str = "Salesforce",
    input_dir: Path | None = None,
    **kwargs,
) -> list[dict[str, Any]]:
    """Process all clips using Grok Imagine Video with cursor overlay prompt.

    Args:
        clips: List of clip dicts from split_video_into_clips.
        user_prompt: Additional user context (from Agent 1 narration).
        platform_name: Platform name for logging.
        input_dir: Directory containing cursor_actions.json.

    Returns:
        List of result dicts with status, path for each clip.
    """
    adapter = get_adapter()
    results: list[dict[str, Any]] = []
    total = len(clips)

    # Build prompt with cursor actions
    if input_dir:
        prompt = build_prompt(input_dir)
        logger.info(f"[Processor] Using cursor metadata from {input_dir}")
    else:
        prompt = GROK_VIDEO_PROMPT.format(
            cursor_actions_block="No cursor action data available. Render cursor at center."
        )

    logger.info(f"[Processor] Processing {total} clips...")

    for clip in clips:
        clip_path = clip["path"]
        clip_duration = max(1, min(int(round(clip["duration"])), 8))
        clip_index = clip["index"]

        output_path = settings.clip_output_dir / f"enhanced_{clip_index:03d}.mp4"

        try:
            result = await _process_with_retry(
                adapter=adapter,
                input_video_path=clip_path,
                prompt=prompt,
                duration=clip_duration,
                output_path=output_path,
                clip_index=clip_index,
            )
        except CreditExhaustedError:
            logger.warning(
                f"[Processor] Credits exhausted at clip {clip_index}. "
                f"Falling back to raw clips for remaining {total - clip_index} clips."
            )
            # Use original split clips for all remaining (including current)
            results.append(_fallback_result(clip_index, clip_path))
            for remaining_clip in clips[clip_index + 1:]:
                results.append(_fallback_result(remaining_clip["index"], remaining_clip["path"]))
            break

        results.append(result)

        status_icon = "✓" if result["status"] in ("success", "dry_run") else "✗"
        logger.info(f"[Processor] Clip {clip_index}/{total - 1} {status_icon}")

    successful = sum(1 for r in results if r["status"] in ("success", "dry_run", "fallback"))
    enhanced = sum(1 for r in results if r["status"] in ("success", "dry_run"))
    fallback_count = sum(1 for r in results if r["status"] == "fallback")
    logger.info(
        f"[Processor] Done | {enhanced}/{total} enhanced | "
        f"{fallback_count}/{total} fallback | {successful}/{total} usable"
    )

    return results


def _fallback_result(clip_index: int, clip_path: str) -> dict[str, Any]:
    """Create a fallback result using the original unprocessed split clip."""
    return {
        "clip_index": clip_index,
        "status": "fallback",
        "path": clip_path,
        "mode": "raw-passthrough",
        "error": "Used original clip (API credits exhausted)",
    }


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

            if _is_credit_exhausted(last_error):
                logger.error(f"[Processor] Clip {clip_index} — credits exhausted.")
                raise CreditExhaustedError(last_error)

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
        "status": "fallback",
        "path": input_video_path,
        "mode": "raw-passthrough",
        "error": last_error,
    }


def _is_credit_exhausted(error_msg: str) -> bool:
    """Detect credit/spending limit errors that should halt the entire pipeline."""
    lower = error_msg.lower()
    return "spending limit" in lower or ("credits" in lower and "permission_denied" in lower)
