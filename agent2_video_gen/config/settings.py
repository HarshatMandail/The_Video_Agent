"""
Application settings loaded from environment variables.
Uses pydantic-settings for validation and type coercion.

Resolves .env relative to THIS file's directory so it works
regardless of which working directory the process runs from.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve paths relative to Agent 2 project root (one level up from config/)
_VIDEO_PIPELINE_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _VIDEO_PIPELINE_DIR.parent
_ENV_FILE = _REPO_ROOT / ".env" if (_REPO_ROOT / ".env").exists() else _VIDEO_PIPELINE_DIR / ".env"
_GENERATED_VIDEOS_DIR = _VIDEO_PIPELINE_DIR / "output" / "generated_videos"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # xAI API Key (required for video generation)
    xai_api_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Video pipeline
    default_model: str = "grok-imagine-video"
    default_resolution: str = "480p"
    max_retries: int = 3
    retry_base_delay: float = 2.0
    clip_output_dir: Path = _GENERATED_VIDEOS_DIR / "clips"
    final_output_dir: Path = _GENERATED_VIDEOS_DIR
    max_clip_duration: int = 8  # Grok API hard limit is ~8.7s, we use 8.0s for safety
    log_level: str = "INFO"

    # Dry run mode — logs prompts without making API calls (saves credits)
    dry_run: bool = False

    # xAI SDK settings
    sdk_generation_timeout: int = 600
    sdk_poll_interval: float = 1.0

    # Tutorial settings
    max_tutorial_steps: int = 15
    default_clip_duration: int = 4
    tutorial_aspect_ratio: str = "16:9"

    # Video preprocessing for edit-video mode
    preprocess_max_duration: int = 6  # Trim clips to max 6 seconds
    preprocess_fps: int = 30  # Normalize FPS (30 or 60)
    preprocess_width: int = 1280  # Output width
    preprocess_height: int = 720  # Output height
    preprocess_output_dir: Path = _GENERATED_VIDEOS_DIR / "preprocessed"

    # Cleanup — remove temporary preprocessed files after final video is produced
    cleanup_temp_files: bool = True


settings = Settings()
