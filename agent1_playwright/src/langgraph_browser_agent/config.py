import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ENV_PATH = _REPO_ROOT / ".env"
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / ".data"

BASE_DIR = _PROJECT_ROOT
SCREENSHOTS_DIR = _DATA_DIR / "screenshots"
BROWSER_DATA_DIR = _DATA_DIR / "browser_data"
CACHE_DIR = _DATA_DIR / "cache"
COST_LOG_DIR = _DATA_DIR / "logs"
AUDIT_LOG_DIR = _DATA_DIR / "logs" / "audit"
AGENT_OUTPUT_DIR = _DATA_DIR / "logs" / "agent_output"
VIDEO_CLIPS_DIR = _DATA_DIR / "video_clips"
OUTPUT_DIR = _DATA_DIR / "video_clips"

for d in [SCREENSHOTS_DIR, BROWSER_DATA_DIR, CACHE_DIR, COST_LOG_DIR, AUDIT_LOG_DIR, AGENT_OUTPUT_DIR, VIDEO_CLIPS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
AZURE_DEPLOYMENT_FULL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_DEPLOYMENT_MINI = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI", AZURE_DEPLOYMENT_FULL)

MAX_TOKENS_PER_REQUEST = int(os.getenv("MAX_TOKENS_PER_REQUEST", "8000"))
MAX_COMPLETION_TOKENS = int(os.getenv("MAX_COMPLETION_TOKENS", "2048"))
MAX_COST_PER_SESSION = float(os.getenv("MAX_COST_PER_SESSION_USD", "1.0"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))

ENABLE_CACHE = os.getenv("ENABLE_LLM_CACHE", "true").lower() == "true"
ENABLE_BUDGET_CHECK = os.getenv("ENABLE_BUDGET_CHECK", "true").lower() == "true"
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "21600"))

RECORD_CURSOR_METADATA = os.getenv("RECORD_CURSOR_METADATA", "true").lower() == "true"
HEADLESS = os.getenv("BROWSER_USE_HEADLESS", "false").lower() == "true"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
SLOW_MO = int(os.getenv("SLOW_MO", "100")) if DEBUG_MODE else 0
NAVIGATION_TIMEOUT_MS = int(os.getenv("NAVIGATION_TIMEOUT_MS", "30000"))
PAGE_LOAD_TIMEOUT_MS = int(os.getenv("PAGE_LOAD_TIMEOUT_MS", "15000"))
WAIT_FOR_LOGIN_TIMEOUT = int(os.getenv("WAIT_FOR_LOGIN_TIMEOUT", "300"))
LOGIN_CHECK_INTERVAL = int(os.getenv("LOGIN_CHECK_INTERVAL", "3"))
BROWSER_USER_AGENT = os.getenv(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

_raw_allowlist = os.getenv("URL_ALLOWLIST", "")
URL_ALLOWLIST: list[str] = [
    d.strip().lower() for d in _raw_allowlist.split(",") if d.strip()
]

_raw_blocklist = os.getenv(
    "URL_BLOCKLIST",
    "localhost,127.0.0.1,0.0.0.0,file://,javascript:,data:,chrome://,about:",
)
URL_BLOCKLIST: list[str] = [
    d.strip().lower() for d in _raw_blocklist.split(",") if d.strip()
]

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

ENABLE_LANGSMITH = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
LANGSMITH_PROJECT = os.getenv("LANGCHAIN_PROJECT", "foxio-agent1")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def validate_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"

    if not parsed.scheme or not parsed.netloc:
        return False, "URL must have scheme and domain"

    if parsed.scheme not in ("http", "https"):
        return False, f"Blocked scheme: {parsed.scheme}"

    domain = parsed.netloc.lower().split(":")[0]

    for blocked in URL_BLOCKLIST:
        if blocked in url.lower():
            return False, f"Blocked domain/pattern: {blocked}"

    if URL_ALLOWLIST:
        allowed = any(
            domain == allowed_domain or domain.endswith(f".{allowed_domain}")
            for allowed_domain in URL_ALLOWLIST
        )
        if not allowed:
            return False, f"Domain '{domain}' not in allowlist"

    return True, "OK"


def validate_config() -> list[str]:
    errors = []
    if not AZURE_OPENAI_ENDPOINT:
        errors.append("AZURE_OPENAI_ENDPOINT is not set")
    if not AZURE_OPENAI_API_KEY:
        errors.append("AZURE_OPENAI_API_KEY is not set")
    return errors
