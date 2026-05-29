# run_pipeline.py — Unified entry point for the Foxio Video Agent pipeline.

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add agent source paths
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "agent1_playwright" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "agent2_video_gen"))

from langgraph_browser_agent import run_full_pipeline, shutdown_browser_pool


async def main(url: str, user_query: str):
    """Run the full Foxio pipeline: Browser Analysis + Recording → Edit-Video."""
    print("=" * 60)
    print("FOXIO — Full Pipeline (Agent 1 → Agent 2)")
    print(f"URL: {url}")
    print(f"Query: {user_query}")
    print("=" * 60)

    result = await run_full_pipeline(
        url=url,
        user_query=user_query,
        cleanup_browser=True,
    )

    print("\n" + "=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"Status: {result['status']}")
    print(f"Stage: {result.get('stage', 'N/A')}")

    if result.get("video_result"):
        vr = result["video_result"]
        print(f"\nVideo Title: {vr.get('video_title', 'N/A')}")
        print(f"Job ID: {vr.get('job_id', 'N/A')}")
        print(f"Final Video: {vr.get('final_video_path', 'N/A')}")
        print(f"Clips: {vr.get('steps_generated', 0)}")

    if result.get("error"):
        print(f"\nError: {result['error']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Foxio Video Agent — Full Pipeline")
    parser.add_argument("--url", required=True, help="Target web application URL")
    parser.add_argument("--query", required=True, help="User task/question to demonstrate")
    parser.add_argument("--no-cursor-overlay", action="store_true", help="Disable cursor overlay")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(main(url=args.url, user_query=args.query))
