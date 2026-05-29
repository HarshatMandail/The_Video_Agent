import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import COST_LOG_DIR, MAX_COST_PER_SESSION

logger = logging.getLogger(__name__)

PRICING = {
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
}


@dataclass
class CallRecord:
    timestamp: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


@dataclass
class SessionUsage:
    session_start: float = field(default_factory=time.time)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    call_count: int = 0
    cache_hits: int = 0
    calls: list[CallRecord] = field(default_factory=list)

    def record_call(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.call_count += 1
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens

        pricing = PRICING.get(model, PRICING["gpt-4o"])
        cost = (prompt_tokens / 1000 * pricing["input"]) + (
            completion_tokens / 1000 * pricing["output"]
        )
        self.total_cost_usd += cost

        self.calls.append(CallRecord(
            timestamp=time.time(),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=round(cost, 6),
        ))

        logger.info(
            f"Call #{self.call_count} cost: ${cost:.4f} | "
            f"Session total: ${self.total_cost_usd:.4f}"
        )

    def record_cache_hit(self) -> None:
        self.cache_hits += 1

    def is_over_budget(self) -> bool:
        return self.total_cost_usd >= MAX_COST_PER_SESSION

    def get_summary(self) -> dict:
        return {
            "call_count": self.call_count,
            "cache_hits": self.cache_hits,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "budget_remaining_usd": round(MAX_COST_PER_SESSION - self.total_cost_usd, 4),
            "duration_seconds": round(time.time() - self.session_start, 1),
            "calls": [
                {
                    "model": c.model,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "cost_usd": c.cost_usd,
                }
                for c in self.calls
            ],
        }

    def save_log(self) -> None:
        log_file = COST_LOG_DIR / f"session_{int(self.session_start)}.json"
        log_file.write_text(
            json.dumps(self.get_summary(), indent=2), encoding="utf-8"
        )
        logger.info(f"Cost log saved: {log_file}")


_current_session: Optional[SessionUsage] = None


def get_session() -> SessionUsage:
    global _current_session
    if _current_session is None:
        _current_session = SessionUsage()
    return _current_session


def reset_session() -> None:
    global _current_session
    if _current_session:
        _current_session.save_log()
    _current_session = SessionUsage()


def estimate_tokens(text: str) -> int:
    return len(text) // 4
