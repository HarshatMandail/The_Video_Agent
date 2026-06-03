"""
LangGraph StateGraph workflow for the video generation pipeline.

Pipeline: enqueue → split → process → concatenate → finalize

Strategy:
  1. Take the single merged raw .mp4 from Agent 1
  2. Split into ≤8.0s clips (Grok API limit)
  3. Process each clip independently via edit-video mode
  4. Concatenate all enhanced clips with cross-fade into final video
  5. Cleanup temp files
"""

import asyncio
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from loguru import logger

from config.settings import settings
from nodes.cursor_overlay import apply_cursor_overlay
from nodes.utils import cleanup_clips, cleanup_preprocessed, concatenate_clips, ensure_directories
from nodes.video_processor import split_video_into_clips, process_clips_sequentially


class PipelineState(TypedDict):
    """State passed between LangGraph nodes."""

    job_id: str
    raw_video_path: str
    user_prompt: str
    platform_name: str
    clips: list[dict[str, Any]]
    clip_results: list[dict[str, Any]]
    final_video_path: str
    status: str
    error: str


def node_enqueue(state: PipelineState) -> dict[str, Any]:
    """Node 1: Validate input and prepare directories."""
    job_id = state.get("job_id") or str(uuid.uuid4())[:8]
    ensure_directories()

    raw_video = state.get("raw_video_path", "")
    user_prompt = state.get("user_prompt", "")

    logger.info(f"[Job {job_id}] Pipeline started | raw_video={raw_video}")

    if not raw_video:
        return {
            "job_id": job_id,
            "status": "failed",
            "error": "No raw_video_path provided.",
        }

    from pathlib import Path
    if not Path(raw_video).exists():
        return {
            "job_id": job_id,
            "status": "failed",
            "error": f"Raw video file not found: {raw_video}",
        }

    return {
        "job_id": job_id,
        "status": "splitting",
    }


def node_split(state: PipelineState) -> dict[str, Any]:
    """Node 2: Split raw video into ≤8.0s clips."""
    if state.get("status") == "failed":
        return {"clips": []}

    job_id = state["job_id"]
    raw_video = state["raw_video_path"]

    logger.info(f"[Job {job_id}] Splitting raw video into clips...")

    try:
        clips = split_video_into_clips(
            raw_video_path=raw_video,
            output_dir=settings.clip_output_dir,
            max_seconds=8.0,
        )
    except RuntimeError as e:
        logger.error(f"[Job {job_id}] Splitting failed: {e}")
        return {"clips": [], "status": "failed", "error": str(e)}

    if not clips:
        return {"clips": [], "status": "failed", "error": "Splitting produced no clips."}

    logger.info(f"[Job {job_id}] Split into {len(clips)} clips")
    return {"clips": clips, "status": "processing"}


def node_process(state: PipelineState) -> dict[str, Any]:
    """Node 3: Process clips with Grok Imagine Video + cursor overlay."""
    if state.get("status") == "failed":
        return {"clip_results": []}

    job_id = state["job_id"]
    clips = state.get("clips", [])
    user_prompt = state.get("user_prompt", "")
    platform_name = state.get("platform_name", "Salesforce")

    # Resolve input directory from raw video path (for cursor_actions.json)
    from pathlib import Path as _Path
    input_dir = _Path(state.get("raw_video_path", "")).parent

    logger.info(f"[Job {job_id}] Processing {len(clips)} clips with cursor overlay...")

    async def _run() -> list[dict[str, Any]]:
        return await process_clips_sequentially(
            clips=clips,
            user_prompt=user_prompt,
            platform_name=platform_name,
            input_dir=input_dir,
        )

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _run())
        clip_results = future.result()

    successful = sum(1 for r in clip_results if r["status"] in ("success", "dry_run"))
    failed = sum(1 for r in clip_results if r["status"] == "failed")

    logger.info(f"[Job {job_id}] Processing complete | success={successful} | failed={failed}")

    return {"clip_results": clip_results, "status": "concatenating"}


def node_concatenate(state: PipelineState) -> dict[str, Any]:
    """Node 4: Concatenate all enhanced clips into final video."""
    job_id = state["job_id"]
    clip_results = state.get("clip_results", [])

    if state.get("status") == "failed":
        return {"final_video_path": "", "status": "failed"}

    if any(r["status"] == "dry_run" for r in clip_results):
        logger.info(f"[Job {job_id}] Dry run — using FFmpeg cursor overlay (Path B) instead.")
        return _fallback_to_cursor_overlay(state)

    usable_statuses = ("success", "fallback")
    successful_paths = [
        r["path"]
        for r in sorted(clip_results, key=lambda x: x["clip_index"])
        if r["status"] in usable_statuses
    ]

    if not successful_paths:
        logger.error(f"[Job {job_id}] No usable clips to concatenate.")
        return {"status": "failed", "error": "All clips failed.", "final_video_path": ""}

    enhanced = sum(1 for r in clip_results if r["status"] == "success")
    fallback = sum(1 for r in clip_results if r["status"] == "fallback")
    logger.info(f"[Job {job_id}] Concatenating {len(successful_paths)} clips ({enhanced} enhanced, {fallback} raw fallback)")

    try:
        final_path = concatenate_clips(successful_paths, job_id)
        return {"final_video_path": str(final_path), "status": "finalizing"}
    except RuntimeError as e:
        logger.error(f"[Job {job_id}] Concatenation failed: {e}")
        return {"status": "failed", "error": str(e), "final_video_path": ""}


def node_finalize(state: PipelineState) -> dict[str, Any]:
    """Node 5: Cleanup temp files."""
    job_id = state["job_id"]
    cleanup_clips()
    cleanup_preprocessed()

    logger.success(f"[Job {job_id}] Pipeline complete | video={state.get('final_video_path')}")
    return {"status": "completed", "error": ""}


def _fallback_to_cursor_overlay(state: PipelineState) -> dict[str, Any]:
    """Apply FFmpeg cursor overlay (Path B) when Grok API is skipped (dry run)."""
    from pathlib import Path

    job_id = state["job_id"]
    raw_video = state["raw_video_path"]
    input_dir = Path(raw_video).parent
    cursor_actions_path = input_dir / "cursor_actions.json"

    if not cursor_actions_path.exists():
        logger.warning(f"[Job {job_id}] No cursor_actions.json found — cannot apply Path B.")
        return {"final_video_path": raw_video, "status": "finalizing"}

    try:
        output = apply_cursor_overlay(
            video_path=raw_video,
            cursor_actions_path=str(cursor_actions_path),
        )
        logger.success(f"[Job {job_id}] Path B cursor overlay complete: {output}")
        return {"final_video_path": output, "status": "finalizing"}
    except Exception as e:
        logger.error(f"[Job {job_id}] Path B failed: {e} — returning raw video.")
        return {"final_video_path": raw_video, "status": "finalizing"}


def build_pipeline_graph() -> StateGraph:
    """Build the LangGraph pipeline: enqueue → split → process → concat → finalize."""
    graph = StateGraph(PipelineState)

    graph.add_node("enqueue", node_enqueue)
    graph.add_node("split", node_split)
    graph.add_node("process", node_process)
    graph.add_node("concatenate", node_concatenate)
    graph.add_node("finalize", node_finalize)

    graph.set_entry_point("enqueue")
    graph.add_edge("enqueue", "split")
    graph.add_edge("split", "process")
    graph.add_edge("process", "concatenate")
    graph.add_edge("concatenate", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


async def run_pipeline(
    raw_video_path: str,
    user_prompt: str = "",
    platform_name: str = "Salesforce",
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    Execute the full video generation pipeline.

    Args:
        raw_video_path: Path to the single merged .mp4 from Agent 1.
        user_prompt: Enhancement/animation prompt for the video.
        platform_name: Platform name for prompt context.
        job_id: Optional custom job ID.

    Returns:
        Final pipeline state with video path and metadata.
    """
    initial_state: PipelineState = {
        "job_id": job_id or str(uuid.uuid4())[:8],
        "raw_video_path": raw_video_path,
        "user_prompt": user_prompt,
        "platform_name": platform_name,
        "clips": [],
        "clip_results": [],
        "final_video_path": "",
        "status": "pending",
        "error": "",
    }

    pipeline = build_pipeline_graph()
    return pipeline.invoke(initial_state)
