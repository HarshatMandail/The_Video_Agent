"""
xAI Grok Imagine Video adapter — clean transport layer.

Single responsibility: upload video, call API, download result.
No prompt logic lives here — prompts are owned by the processor layer.
"""

import base64
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from xai_sdk import Client

from adapters.base import VideoGenerationService
from config.settings import settings

SUPPORTED_ASPECT_RATIOS = {"1:1", "16:9", "9:16"}
SUPPORTED_RESOLUTIONS = {"480p", "720p"}


class GrokAdapter(VideoGenerationService):
    """
    xAI Grok Imagine Video adapter.

    Pure transport — accepts a prompt and video, calls the API, returns the result.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def generate_video(
        self,
        input_video_path: str,
        prompt: str,
        duration: int = 8,
        output_path: Path | None = None,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
    ) -> dict[str, Any]:
        """
        Enhance a video clip using Grok Imagine Video edit-video mode.

        Args:
            input_video_path: Path to the source video file.
            prompt: The full prompt (caller owns prompt content).
            duration: Target duration in seconds (clamped to API limit).
            output_path: Where to save the result.
            aspect_ratio: Video aspect ratio.
            resolution: Output resolution.

        Returns:
            Dict with status, path, duration, mode, cost_usd, etc.
        """
        video_file = Path(input_video_path)
        if not video_file.exists():
            raise FileNotFoundError(f"Input video not found: {input_video_path}")

        if output_path is None:
            output_path = video_file.with_suffix(".enhanced.mp4")

        clamped_duration = max(1, min(duration, settings.max_clip_duration))

        logger.info(
            f"[GrokAdapter] edit-video | input={video_file.name} | "
            f"duration={clamped_duration}s"
        )

        if settings.dry_run:
            return self._dry_run_result(output_path, video_file, clamped_duration)

        data_url = self._encode_video(video_file)

        validated_ar = aspect_ratio if aspect_ratio in SUPPORTED_ASPECT_RATIOS else "16:9"
        validated_res = resolution if resolution in SUPPORTED_RESOLUTIONS else "480p"

        gen_kwargs = {
            "model": "grok-imagine-video",
            "prompt": prompt,
            "duration": clamped_duration,
            "aspect_ratio": validated_ar,
            "resolution": validated_res,
            "video_url": data_url,
        }

        logger.info("[GrokAdapter] Calling xAI API...")

        client = Client(api_key=self._api_key)
        response = client.video.generate(**gen_kwargs)

        video_bytes = await self._download_video(response.url)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(video_bytes)

        response_duration = getattr(response, "duration", clamped_duration)

        logger.success(
            f"[GrokAdapter] Saved: {output_path.name} | "
            f"duration={response_duration}s | "
            f"size={output_path.stat().st_size // 1024}KB"
        )

        return {
            "status": "success",
            "path": str(output_path),
            "model": "grok-imagine-video",
            "duration": response_duration,
            "mode": "edit-video",
            "cost_usd": getattr(response, "cost_usd", None),
        }

    # ─── Private Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _encode_video(video_path: Path) -> str:
        """Encode video as base64 data URL for the xAI SDK."""
        mime_type = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
        file_size_kb = video_path.stat().st_size // 1024

        logger.info(f"[GrokAdapter] Encoding {video_path.name} ({file_size_kb}KB) as base64")

        video_bytes = video_path.read_bytes()
        b64 = base64.b64encode(video_bytes).decode("ascii")
        return f"data:{mime_type};base64,{b64}"

    @staticmethod
    def _dry_run_result(output_path: Path, input_file: Path, duration: int) -> dict[str, Any]:
        """Return a dry-run result without calling the API."""
        logger.warning(f"[GrokAdapter] DRY RUN | input={input_file.name}")
        return {
            "status": "dry_run",
            "path": str(output_path),
            "model": "grok-imagine-video",
            "duration": duration,
            "mode": "edit-video",
            "cost_usd": 0.0,
        }

    @staticmethod
    async def _download_video(url: str) -> bytes:
        """Download generated video from temporary URL."""
        async with httpx.AsyncClient(timeout=180.0) as http_client:
            resp = await http_client.get(url)
            resp.raise_for_status()
            return resp.content
