import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LOG_LEVEL, AUDIT_LOG_DIR


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


class AuditLogger:
    """Records detailed audit trail of agent actions."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.start_time = time.time()
        self.events: list[dict[str, Any]] = []
        self._logger = logging.getLogger("audit")

    def log(self, action: str, details: dict[str, Any] | None = None) -> None:
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "elapsed_s": round(time.time() - self.start_time, 2),
            "action": action,
            "details": details or {},
        }
        self.events.append(event)
        self._logger.info(f"[{self.session_id}] {action}: {details}")

    def save(self) -> Path:
        log_file = AUDIT_LOG_DIR / f"audit_{self.session_id}.json"
        log_file.write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
                    "duration_s": round(time.time() - self.start_time, 2),
                    "event_count": len(self.events),
                    "events": self.events,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return log_file
