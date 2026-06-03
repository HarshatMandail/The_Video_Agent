"""
FFmpeg Cursor Overlay — Programmatic cursor compositing on raw video.

Reads cursor_actions.json (from Agent 1) and overlays a cursor PNG at the
correct positions/times using FFmpeg filter_complex. Optionally renders a
subtle click ripple effect.

This replaces the unreliable Grok Imagine Video cursor overlay with a
deterministic, pixel-perfect approach.

Usage:
    from nodes.cursor_overlay import apply_cursor_overlay

    output = apply_cursor_overlay(
        video_path="path/to/raw_video.mp4",
        cursor_actions_path="path/to/cursor_actions.json",
    )
    print(f"Output: {output}")
"""

import json
import subprocess
from pathlib import Path

from loguru import logger

from nodes.utils import probe_duration

# Default cursor PNG — white arrow with dark outline (32x32)
DEFAULT_CURSOR_PATH = Path(__file__).parent.parent / "assets" / "cursor.png"

# Cursor display size (scales the 32x32 PNG to this for visibility)
CURSOR_DISPLAY_SIZE = 48
RIPPLE_RADIUS = 20
RIPPLE_DURATION_S = 0.18
RIPPLE_COLOR = "0x4285F480"  # Google blue, ~50% opacity


def apply_cursor_overlay(
    video_path: str,
    cursor_actions_path: str,
    output_path: str | None = None,
    cursor_image: str | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
    show_ripple: bool = True,
) -> str:
    """
    Overlay a cursor on the video using FFmpeg based on cursor_actions.json.

    Args:
        video_path: Path to the raw input video.
        cursor_actions_path: Path to cursor_actions.json from Agent 1.
        output_path: Output video path (defaults to <input>_cursor.mp4).
        cursor_image: Path to cursor PNG (defaults to built-in asset).
        video_width: Override video width for coordinate scaling.
        video_height: Override video height for coordinate scaling.
        show_ripple: Whether to render click ripple effects.

    Returns:
        Path to the output video with cursor overlay.
    """
    input_path = Path(video_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input video not found: {video_path}")

    actions = _load_actions(cursor_actions_path)
    if not actions:
        logger.warning("[CursorOverlay] No valid actions found — copying video unchanged.")
        return video_path

    # Resolve output path — default to same directory as Path A (output/generated_videos/)
    if output_path is None:
        from config.settings import settings
        settings.final_output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(settings.final_output_dir / f"{input_path.stem}_cursor.mp4")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Resolve cursor image
    cursor_img = Path(cursor_image) if cursor_image else DEFAULT_CURSOR_PATH
    if not cursor_img.exists():
        logger.info("[CursorOverlay] Cursor PNG not found — generating default.")
        _generate_default_cursor(cursor_img)

    # Probe video dimensions if not provided
    if not video_width or not video_height:
        w, h = _probe_resolution(input_path)
        video_width = video_width or w
        video_height = video_height or h

    # Build and run FFmpeg command
    filter_complex = build_cursor_filter(
        actions=actions,
        video_width=video_width,
        video_height=video_height,
        show_ripple=show_ripple,
    )

    # Probe input duration to cap output length (prevents -loop 1 from extending)
    video_duration = probe_duration(input_path) or 300

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-loop", "1", "-t", f"{video_duration:.3f}", "-i", str(cursor_img),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info(f"[CursorOverlay] Overlaying {len(actions)} actions onto {input_path.name}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        logger.success(f"[CursorOverlay] Output: {output_path}")
    except subprocess.CalledProcessError as e:
        logger.error(f"[CursorOverlay] FFmpeg failed: {e.stderr[:500]}")
        raise RuntimeError(f"Cursor overlay failed: {e.stderr[:200]}") from e
    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found. Install FFmpeg and ensure it's in PATH.")

    return output_path


def build_cursor_filter(
    actions: list[dict],
    video_width: int = 1280,
    video_height: int = 720,
    show_ripple: bool = True,
) -> str:
    """
    Build FFmpeg filter_complex string that moves a cursor overlay across frames.

    Strategy: Use the overlay filter with enable expressions based on timestamps.
    For each action, we enable the cursor overlay at its position between this
    action's timestamp and the next action's timestamp (interpolating would require
    complex expressions, so we use discrete position updates).

    Args:
        actions: List of cursor action dicts with timestamp, x, y, action fields.
        video_width: Video width for clamping coordinates.
        video_height: Video height for clamping coordinates.
        show_ripple: Whether to add click ripple drawbox filters.

    Returns:
        FFmpeg filter_complex string.
    """
    if not actions:
        return "[0:v]copy[out]"

    # Sort by timestamp
    actions = sorted(actions, key=lambda a: a.get("timestamp", 0))

    # Build overlay segments — cursor jumps between positions at each timestamp
    # Using sendcmd/overlay approach: one overlay with dynamic x,y via enable ranges
    # Simpler approach: chain of overlay filters with enable='between(t, start, end)'
    # For performance, we batch into a single overlay using expression-based x/y.

    # Build x(t) and y(t) expressions using nested if() for each segment
    x_expr = _build_position_expr(actions, "x", video_width, cursor_offset=0)
    y_expr = _build_position_expr(actions, "y", video_height, cursor_offset=0)

    # Main cursor overlay with dynamic position
    # Scale cursor for visibility, then overlay with expression-based positioning
    # shortest=0 ensures output matches the video duration (not the looped image)
    filter_parts = [
        f"[1:v]scale={CURSOR_DISPLAY_SIZE}:{CURSOR_DISPLAY_SIZE}:flags=lanczos[cur]",
        f"[0:v][cur]overlay=x='{x_expr}':y='{y_expr}':shortest=0:eof_action=endall[cursored]",
    ]

    # Add click ripple effects if enabled
    if show_ripple:
        click_actions = [a for a in actions if a.get("action") == "click"]
        if click_actions:
            ripple_filter = _build_ripple_filter(click_actions, video_width, video_height)
            filter_parts.append(f"[cursored]{ripple_filter}[out]")
        else:
            filter_parts.append("[cursored]copy[out]")
    else:
        filter_parts.append("[cursored]copy[out]")

    return ";".join(filter_parts)


def _build_position_expr(
    actions: list[dict],
    axis: str,
    max_val: int,
    cursor_offset: int = 0,
) -> str:
    """
    Build an FFmpeg expression for cursor x or y position over time.

    Uses nested if(gte(t,timestamp), value, ...) to step between positions.
    Built from LAST timestamp to FIRST so the outermost check is the latest time.
    This ensures correct evaluation: FFmpeg evaluates the first true condition.
    """
    if not actions:
        return str(max_val // 2)

    # Clamp coordinates to video bounds, sort by timestamp ascending
    positions = []
    for a in sorted(actions, key=lambda a: a.get("timestamp", 0)):
        val = int(a.get(axis, 0)) - cursor_offset
        val = max(0, min(val, max_val - CURSOR_DISPLAY_SIZE))  # cursor image size
        positions.append((a.get("timestamp", 0), val))

    # Build nested if expression — outermost = latest timestamp
    # Logic: if(gte(t, t_last), pos_last, if(gte(t, t_prev), pos_prev, ... pos_first))
    # Start with the first position as the default (before any action)
    expr = str(positions[0][1])

    # Wrap from second position onward (skip first since it's the default)
    # Build inside-out: start from second-to-last, wrap outward to last
    for ts, pos in positions[1:]:
        expr = f"if(gte(t\\,{ts:.3f})\\,{pos}\\,{expr})"

    return expr


def _build_ripple_filter(
    click_actions: list[dict],
    video_width: int,
    video_height: int,
) -> str:
    """
    Build drawbox-based click ripple effects for each click action.

    Uses drawbox with circular approximation + enable='between(t, start, end)'.
    """
    filters = []
    for i, action in enumerate(click_actions):
        ts = action.get("timestamp", 0)
        x = max(0, min(int(action.get("x", 0)), video_width - RIPPLE_RADIUS))
        y = max(0, min(int(action.get("y", 0)), video_height - RIPPLE_RADIUS))

        # Draw a small semi-transparent circle using drawbox (approximation)
        # For a proper circle, we'd need to generate a ripple PNG per click,
        # but drawbox is simpler and still looks subtle enough.
        x1 = max(0, x - RIPPLE_RADIUS)
        y1 = max(0, y - RIPPLE_RADIUS)
        size = RIPPLE_RADIUS * 2

        end_ts = ts + RIPPLE_DURATION_S
        filters.append(
            f"drawbox=x={x1}:y={y1}:w={size}:h={size}:"
            f"color={RIPPLE_COLOR}:t=2:"
            f"enable='between(t,{ts:.3f},{end_ts:.3f})'"
        )

    if not filters:
        return "copy"

    return ",".join(filters)


def _load_actions(path: str) -> list[dict]:
    """Load and validate cursor_actions.json."""
    actions_path = Path(path)
    if not actions_path.exists():
        logger.warning(f"[CursorOverlay] cursor_actions.json not found: {path}")
        return []

    try:
        data = json.loads(actions_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []

        valid = [
            a for a in data
            if isinstance(a, dict)
            and "timestamp" in a
            and "x" in a
            and "y" in a
        ]
        logger.info(f"[CursorOverlay] Loaded {len(valid)} cursor actions from {actions_path.name}")
        return valid

    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[CursorOverlay] Failed to read actions: {e}")
        return []


def _probe_resolution(video_path: Path) -> tuple[int, int]:
    """Probe video width and height using FFprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "v:0",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        return int(stream.get("width", 1280)), int(stream.get("height", 720))
    except Exception as e:
        logger.warning(f"[CursorOverlay] FFprobe resolution failed, using 1280x720: {e}")
        return 1280, 720


def _generate_default_cursor(output_path: Path) -> None:
    """
    Generate a white arrow cursor PNG (32x32) with black outline using Python.
    No external dependencies — writes raw PNG bytes directly.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Define a 32x32 cursor bitmap — 1=white, 2=black outline, 0=transparent
    # Classic Windows-style arrow cursor
    cursor_data = [
        [1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,2,1,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,2,1,0,1,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,1,0,0,1,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [1,0,0,0,0,1,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [0,0,0,0,0,1,2,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [0,0,0,0,0,0,1,2,2,2,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
        [0,0,0,0,0,0,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    ]

    # Pad to 32 rows
    while len(cursor_data) < 32:
        cursor_data.append([0] * 32)

    # Color map: 0=transparent, 1=black outline, 2=white fill
    color_map = {
        0: (0, 0, 0, 0),
        1: (0, 0, 0, 255),
        2: (255, 255, 255, 255),
    }

    # Write raw PNG
    _write_png(output_path, cursor_data, color_map, width=32, height=32)
    logger.info(f"[CursorOverlay] Generated default cursor: {output_path}")


def _write_png(path: Path, pixels: list[list[int]], color_map: dict, width: int, height: int) -> None:
    """Write a minimal RGBA PNG file from pixel data."""
    import struct
    import zlib

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    ihdr = chunk(b"IHDR", ihdr_data)

    # IDAT — raw pixel rows with filter byte 0 (None) per row
    raw_data = b""
    for row in pixels[:height]:
        raw_data += b"\x00"  # filter byte
        for col in row[:width]:
            r, g, b, a = color_map.get(col, (0, 0, 0, 0))
            raw_data += struct.pack("BBBB", r, g, b, a)

    idat = chunk(b"IDAT", zlib.compress(raw_data))

    # IEND
    iend = chunk(b"IEND", b"")

    # Write file
    png_signature = b"\x89PNG\r\n\x1a\n"
    path.write_bytes(png_signature + ihdr + idat + iend)
