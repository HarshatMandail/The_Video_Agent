import hashlib
import json
import logging
import time
from typing import Optional

from .config import CACHE_DIR, CACHE_TTL

logger = logging.getLogger(__name__)


def _generate_cache_key(system_prompt: str, user_message: str) -> str:
    content = f"{system_prompt}|{user_message}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def get_cached_response(system_prompt: str, user_message: str) -> Optional[str]:
    key = _generate_cache_key(system_prompt, user_message)
    cache_file = CACHE_DIR / f"{key}.json"

    if not cache_file.exists():
        return None

    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if time.time() - data.get("cached_at", 0) > CACHE_TTL:
            cache_file.unlink()
            return None
        logger.info(f"Cache hit: {key}")
        return data["response"]
    except (json.JSONDecodeError, KeyError):
        cache_file.unlink(missing_ok=True)
        return None


def save_to_cache(
    system_prompt: str,
    user_message: str,
    response: str,
    tokens_used: int = 0,
) -> None:
    key = _generate_cache_key(system_prompt, user_message)
    cache_file = CACHE_DIR / f"{key}.json"

    data = {
        "cached_at": time.time(),
        "response": response,
        "tokens_saved": tokens_used,
    }
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def clear_cache() -> int:
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        count += 1
    logger.info(f"Cache cleared: {count} entries removed")
    return count
