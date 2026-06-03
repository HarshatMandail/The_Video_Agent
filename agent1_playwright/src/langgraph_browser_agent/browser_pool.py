# browser_pool.py — Browser Pooling with Video Recording
import asyncio
import logging
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

from .config import (
    BROWSER_CHANNEL,
    HEADLESS,
    SLOW_MO,
    VIDEO_CLIPS_DIR,
)

logger = logging.getLogger(__name__)


class BrowserPool:
    """
    Manages browser contexts with video recording.
    Login happens in a non-recording context. After login, a NEW recording
    context is created so the video only contains the actual task.
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._lock = asyncio.Lock()
        self._is_recording = False

    async def acquire(self) -> tuple[BrowserContext, Page]:
        """Get or create a browser context and page (starts without recording)."""
        async with self._lock:
            if self._context is None:
                await self._launch(recording=False)

            try:
                pages = self._context.pages
                page = pages[0] if pages else await self._context.new_page()
                _ = page.url
                return self._context, page
            except Exception:
                logger.warning("Browser context stale, relaunching...")
                await self._cleanup()
                await self._launch(recording=False)
                page = (
                    self._context.pages[0]
                    if self._context.pages
                    else await self._context.new_page()
                )
                return self._context, page

    async def restart_with_recording(self) -> tuple[BrowserContext, Page]:
        """
        Close the current (non-recording) context and create a new one WITH
        video recording enabled. Carries over cookies only for auth.
        """
        async with self._lock:
            if self._is_recording:
                pages = self._context.pages
                page = pages[0] if pages else await self._context.new_page()
                return self._context, page

            storage_state = None
            if self._context:
                try:
                    full_state = await self._context.storage_state()
                    storage_state = {
                        "cookies": full_state.get("cookies", []),
                        "origins": [],
                    }
                except Exception:
                    pass
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None

            await self._create_context(recording=True, storage_state=storage_state)
            page = await self._context.new_page()
            logger.info("New recording context started (no login footage)")
            return self._context, page

    async def _launch(self, recording: bool = False) -> None:
        """Launch browser and create initial context."""
        self._playwright = await async_playwright().start()

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-infobars",
            "--disable-popup-blocking",
            "--hide-scrollbars",
            "--window-size=1280,720",
        ]

        launch_opts = {
            "headless": HEADLESS,
            "slow_mo": SLOW_MO,
            "args": browser_args,
            "ignore_default_args": ["--enable-automation"],
        }

        if BROWSER_CHANNEL:
            launch_opts["channel"] = BROWSER_CHANNEL

        self._browser = await self._playwright.chromium.launch(**launch_opts)

        # Load saved cookies (if available) to skip login
        import os
        state_path = "salesforce_state.json"
        storage_state = state_path if os.path.exists(state_path) else None

        await self._create_context(recording=recording, storage_state=storage_state)
        logger.info(f"Browser launched | slow_mo={SLOW_MO}ms | headless={HEADLESS} | recording={recording}")

    async def _create_context(self, recording: bool = False, storage_state=None) -> None:
        """Create a browser context with optional video recording."""
        context_opts = {
            "viewport": {"width": 1280, "height": 720},
            "ignore_https_errors": True,
            "extra_http_headers": {"Accept-Language": "en-US"},
        }

        if storage_state:
            context_opts["storage_state"] = storage_state

        if recording:
            context_opts["record_video_dir"] = str(VIDEO_CLIPS_DIR)
            context_opts["record_video_size"] = {"width": 1280, "height": 720}
            self._is_recording = True
            logger.info("Video recording ENABLED (720p)")
        else:
            self._is_recording = False
            logger.debug("Video recording DISABLED (login phase)")

        self._context = await self._browser.new_context(**context_opts)

        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    async def release(self, context=None):
        """Only close context after the full task is done."""
        pass

    async def shutdown(self) -> None:
        """Close browser context and playwright."""
        async with self._lock:
            await self._cleanup()

    async def _cleanup(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception as e:
                logger.warning(f"Browser close error (non-fatal): {e}")
            self._context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._is_recording = False


# Module-level singleton
_pool: Optional[BrowserPool] = None


def get_browser_pool() -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool


async def shutdown_browser_pool() -> None:
    global _pool
    if _pool:
        await _pool.shutdown()
        _pool = None


async def save_login_state(context):
    """Save only cookies (not localStorage) so login persists without tab state issues."""
    import json
    state_path = "salesforce_state.json"
    try:
        full_state = await context.storage_state()
        cookies_only = {
            "cookies": full_state.get("cookies", []),
            "origins": [],
        }
        with open(state_path, "w") as f:
            json.dump(cookies_only, f)
        logger.info(f"Saved login cookies to {state_path}")
    except Exception as e:
        logger.warning(f"Could not save login state: {e}")
