import json
import logging
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from .config import OUTPUT_DIR

logger = logging.getLogger(__name__)

_CAPTURE_SCRIPT = """
() => {
  if (window.__cursorRecorderAttached) return;
  window.__cursorRecorderAttached = true;

  document.addEventListener('click', (e) => {
    const el = e.target;
    const label = el.getAttribute('aria-label')
      || el.getAttribute('title')
      || el.innerText?.trim().slice(0, 50)
      || el.tagName.toLowerCase();
    window.__reportCursorAction(JSON.stringify({
      action: 'click',
      x: e.clientX,
      y: e.clientY,
      element: label,
    }));
  }, true);

  document.addEventListener('mousemove', (() => {
    let last = 0;
    return (e) => {
      const now = Date.now();
      if (now - last < 500) return;
      last = now;
      window.__reportCursorAction(JSON.stringify({
        action: 'hover',
        x: e.clientX,
        y: e.clientY,
        element: '',
      }));
    };
  })(), true);

  document.addEventListener('keydown', (e) => {
    if (e.key.length > 1 && !['Enter', 'Tab', 'Backspace', 'Escape'].includes(e.key)) return;
    const el = e.target;
    const label = el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.tagName.toLowerCase();
    window.__reportCursorAction(JSON.stringify({
      action: 'keypress',
      x: 0,
      y: 0,
      element: label,
      key: e.key.length === 1 ? e.key : `[${e.key}]`,
    }));
  }, true);
}
"""


class CursorRecorder:
    """Records cursor and keyboard actions during a Playwright session."""

    def __init__(self):
        self._actions: list[dict] = []
        self._start_time: Optional[float] = None
        self._attached = False

    async def attach(self, page: Page) -> None:
        if self._attached:
            return

        self._start_time = time.time()
        await page.expose_function('__reportCursorAction', self._handle_action)
        await page.evaluate(_CAPTURE_SCRIPT)
        page.on('load', lambda _: page.evaluate(_CAPTURE_SCRIPT))

        self._attached = True
        logger.info("CursorRecorder attached to page.")

    def _handle_action(self, raw_json: str) -> None:
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            return

        elapsed = round(time.time() - self._start_time, 2) if self._start_time else 0.0

        record = {
            "timestamp": elapsed,
            "action": data.get("action", "unknown"),
            "x": data.get("x", 0),
            "y": data.get("y", 0),
            "element": data.get("element", ""),
        }

        if data.get("key"):
            record["key"] = data["key"]

        record["description"] = _build_description(record)
        self._actions.append(record)

    @property
    def actions(self) -> list[dict]:
        return self._actions

    def save(self, output_dir: Optional[Path] = None) -> Path:
        target_dir = output_dir or OUTPUT_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / "cursor_actions.json"

        output_path.write_text(
            json.dumps(self._actions, indent=2),
            encoding="utf-8",
        )

        logger.info(f"CursorRecorder saved {len(self._actions)} actions to {output_path}")
        return output_path


def _build_description(record: dict) -> str:
    action = record["action"]
    element = record.get("element", "").strip()

    if action == "click":
        target = element or f"({record['x']}, {record['y']})"
        return f"Clicked {target}"
    if action == "hover":
        return f"Mouse moved to ({record['x']}, {record['y']})"
    if action == "keypress":
        key = record.get("key", "")
        target = f" in {element}" if element else ""
        return f"Typed '{key}'{target}"
    return f"{action} at ({record['x']}, {record['y']})"
