"""
Video Splitter — Splits a raw recording into overlapping clips for Grok API.

Grok Imagine Video has a ~8.7s limit per call. We create 8s clips
with 1s overlap for smooth cross-fade transitions during concatenation.

Also provides the GROK_IMAGINE_VIDEO_EDIT_PROMPT for full raw video edit mode,
which uses cursor_actions.json to overlay precise cursor movements and clicks.
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from config.settings import settings
from nodes.utils import probe_duration

CLIP_DURATION = 8.0
OVERLAP_SECONDS = 1.0
STEP_SECONDS = CLIP_DURATION - OVERLAP_SECONDS


# ─── Grok Imagine Video Edit Mode Prompt ──────────────────────────────────────
# This prompt is used when processing the FULL raw video as a single unit
# (instead of splitting into clips). It instructs Grok to overlay a natural
# mouse cursor with click animations, guided by cursor_actions.json timestamps.
#
# IMPORTANT: This prompt is intentionally GENERIC — it works for any web
# application workflow. Do NOT add platform-specific references or branding.

GROK_IMAGINE_VIDEO_EDIT_PROMPT = """\
STRICT VIDEO EDIT MODE — DO NOT MODIFY THE ORIGINAL RECORDING

You are editing a raw screen recording of a real web application workflow.
Your ONLY job is to overlay a realistic mouse cursor and click animations.
The original video content must remain 100% unchanged.

## OUTPUT SPECIFICATIONS
- Resolution: 1280x720 (720p)
- Aspect ratio: 16:9
- Preserve the FULL original duration of the source video — do NOT shorten, trim, or speed up
- Frame rate: 30fps
- No audio, no voiceover, no captions, no extra text

## CURSOR RENDERING RULES
- Use only a standard white desktop arrow cursor with dark border and subtle drop shadow
- Cursor size: ~20px, consistent across all frames
- Movement must be smooth, natural, and realistic (ease-in/ease-out, slight bezier curves)
- Speed should feel human but clear and tutorial-friendly — never robotic or too fast

## CLICK ANIMATION RULES
- On every "click" action from cursor_actions.json: show a clean circular ripple/pulse
- Ripple: expanding circle from exact click coordinates, 150-200ms duration
- Color: rgba(66, 133, 244, 0.6) — subtle blue highlight
- Ripple max radius: 24px
- After click, cursor can stay or move naturally to the next action

## CURSOR ACTIONS TIMELINE
{cursor_actions_block}

## STRICT RULES — ZERO HALLUCINATION ALLOWED
- Keep every single pixel of the original recording EXACTLY as-is
- Do NOT change, move, add, or remove ANY UI element, text, button, layout, color, or font
- Do NOT add logos, watermarks, arrows, highlights, tooltips, or any creative elements
- Do NOT zoom, pan, add camera movement, or apply cinematic effects
- Follow timestamps in cursor_actions.json EXACTLY — map them proportionally to the full video length
- Between actions, move the cursor smoothly toward the next click target
- If no cursor_actions.json is provided, render a stationary cursor at center of screen

## NEGATIVE PROMPT
human hand, finger, glowing cursor, cartoon cursor, extra text, changed text, hallucinated labels, layout changes, distorted text, unreadable UI, cinematic effects, zoom, pan, transition, fade, fast movement, rushed pacing, added elements, branding, logo, watermark

## STYLE
Professional enterprise SaaS screen recording. Clean, sharp, polished, and realistic. Perfect text clarity. No AI artifacts. Tutorial-ready quality.
"""


def load_cursor_actions(input_dir: Path) -> Optional[list[dict]]:
    """
    Load cursor_actions.json from the input directory if it exists.

    Handles missing files, empty files, invalid JSON, and non-list content
    gracefully — always returns a valid list or None.

    Args:
        input_dir: Directory containing the raw video and optional cursor metadata.

    Returns:
        List of cursor action dicts, or None if unavailable/invalid.
    """
    cursor_file = input_dir / "cursor_actions.json"
    if not cursor_file.exists():
        logger.info("[Splitter] No cursor_actions.json found — cursor will use fallback positioning.")
        return None

    try:
        raw_text = cursor_file.read_text(encoding="utf-8").strip()
        if not raw_text:
            logger.warning("[Splitter] cursor_actions.json is empty — using fallback.")
            return None

        data = json.loads(raw_text)

        if not isinstance(data, list):
            logger.warning("[Splitter] cursor_actions.json is not a JSON array — using fallback.")
            return None

        # Filter out malformed entries
        valid_actions = [
            a for a in data
            if isinstance(a, dict) and "timestamp" in a and "action" in a
        ]

        if not valid_actions:
            logger.warning("[Splitter] cursor_actions.json has no valid action entries — using fallback.")
            return None

        logger.info(f"[Splitter] Loaded {len(valid_actions)} cursor actions from {cursor_file}")
        return valid_actions

    except json.JSONDecodeError as e:
        logger.warning(f"[Splitter] cursor_actions.json has invalid JSON: {e}")
        return None
    except OSError as e:
        logger.warning(f"[Splitter] Cannot read cursor_actions.json: {e}")
        return None


def build_video_edit_prompt(input_dir: Path) -> str:
    """
    Build the full Grok Imagine Video Edit prompt, injecting cursor timing
    instructions from cursor_actions.json if available.

    This function is completely generic — it works for any web application
    workflow recording regardless of the platform being captured.

    Args:
        input_dir: Directory containing raw video and optional cursor_actions.json.

    Returns:
        Fully rendered prompt string ready for the Grok API.
    """
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

    return GROK_IMAGINE_VIDEO_EDIT_PROMPT.format(cursor_actions_block=cursor_block)


# ─── Clip Splitting (existing functionality) ─────────────────────────────────


def split_video_into_clips(
    raw_video_path: str,
    output_dir: Path | None = None,
    max_seconds: float = CLIP_DURATION,
) -> list[dict]:
    """
    Split a raw .mp4 video into overlapping clips for Grok API.

    Each clip is 8s long. Consecutive clips overlap by 1s:
      clip_0: 0.0 - 8.0s
      clip_1: 7.0 - 15.0s
      clip_2: 14.0 - 22.0s

    Args:
        raw_video_path: Path to the merged raw .mp4 from Agent 1.
        output_dir: Directory to write clip files.
        max_seconds: Maximum duration per clip (default 8.0s).

    Returns:
        List of dicts with keys: index, path, start_time, duration.

    Raises:
        RuntimeError: If FFmpeg fails or input doesn't exist.
    """
    input_path = Path(raw_video_path)
    if not input_path.exists():
        raise RuntimeError(f"[Splitter] Input video not found: {raw_video_path}")

    out_dir = output_dir or settings.clip_output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    total_duration = probe_duration(input_path)
    if not total_duration or total_duration <= 0:
        raise RuntimeError(f"[Splitter] Cannot determine video duration: {raw_video_path}")

    clip_starts = _calculate_clip_starts(total_duration)

    logger.info(
        f"[Splitter] Splitting {input_path.name} ({total_duration:.1f}s) "
        f"into {len(clip_starts)} clips of \u2264{max_seconds}s with {OVERLAP_SECONDS}s overlap"
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

    logger.info(f"[Splitter] Created {len(clips)} clips from {total_duration:.1f}s source")
    return clips


def _calculate_clip_starts(total_duration: float) -> list[float]:
    """Calculate clip start times with overlap."""
    starts = []
    t = 0.0
    while t < total_duration:
        starts.append(t)
        t += STEP_SECONDS
        if t >= total_duration and (total_duration - starts[-1]) < 1.0:
            break
    return starts


def _extract_clip(input_path: Path, output_file: Path, start: float, duration: float, index: int) -> None:
    """Extract a single clip using FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
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
        raise RuntimeError("[Splitter] FFmpeg not found. Install FFmpeg.")
