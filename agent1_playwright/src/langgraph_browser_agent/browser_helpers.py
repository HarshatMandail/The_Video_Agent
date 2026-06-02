import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from playwright.async_api import Page

from .config import SCREENSHOTS_DIR
from .models import NavigationStep, PageCapture

logger = logging.getLogger(__name__)


async def is_classic_fallback_page(page: Page) -> bool:
    """Detect the Salesforce Classic fallback page that can't render in Lightning.

    Some Salesforce trial orgs have Home tab overrides pointing to Visualforce pages.
    When this happens, Lightning shows a blank page with the message:
    "You can't view this item in Lightning Experience. Open in Salesforce Classic."

    This wastes recording frames, so we detect it early and redirect.
    """
    try:
        content = await page.text_content("body", timeout=3000)
        if not content:
            return False
        return "You can't view this item in Lightning Experience" in content
    except Exception:
        pass

    # Fallback: check via title or known selectors when text_content fails
    # (e.g. page is inside an iframe or shadow DOM)
    try:
        title = await page.title()
        if "classic" in title.lower() or "visualforce" in title.lower():
            return True
    except Exception:
        pass

    try:
        # Salesforce shows a specific link to switch to Classic
        classic_link = page.locator('a:has-text("Salesforce Classic"), a:has-text("Switch to Salesforce Classic")')
        if await classic_link.first.is_visible(timeout=2000):
            return True
    except Exception:
        pass

    return False


async def should_start_recording(page) -> bool:
    """Return True when past the login page (any authenticated app page)."""
    url = page.url.lower()
    if not url or url in ("about:blank", "chrome://newtab/"):
        return False
    login_indicators = ["/login", "/signin", "/sign-in", "/sso", "/oauth", "/authorize", "login.salesforce.com"]
    return not any(ind in url for ind in login_indicators)


async def _has_active_form_modal(page: Page) -> bool:
    return await page.evaluate("""() => {
        const dialogs = document.querySelectorAll('[role="dialog"], .slds-modal, .uiModal, .forceModalContainer');
        for (const d of dialogs) {
            const hasForm = d.querySelector('input, select, textarea, form, records-lwc-record-layout, records-record-layout-event-broker');
            if (hasForm) return true;
            const text = (d.textContent || '').toLowerCase();
            if (text.includes('save') || text.includes('required') || text.includes('field')) return true;
        }
        return false;
    }""")


async def dismiss_common_popups(page):
    """Auto-dismiss promotional popups (NOT form modals)."""
    try:
        if await _has_active_form_modal(page):
            return
    except Exception:
        pass

    selectors = [
        "button:has-text('Dismiss')",
        "button:has-text('Got it')",
        "button:has-text('Skip')",
        "button:has-text('Not Now')",
        "button.slds-popover__close",
        "button.toastClose",
        "[data-testid*='dismiss']",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=800):
                await loc.click(timeout=2000)
                await asyncio.sleep(0.3)
        except Exception:
            pass


def _safe_filename(text: str, max_len: int = 40) -> str:
    return "".join(
        c if c.isalnum() or c in ("_", "-") else "_"
        for c in text[:max_len]
    ).strip("_") or "page"


_DOM_EXTRACTION_SCRIPT = """() => JSON.stringify({
    title: document.title,
    h1: document.querySelector('h1') ? document.querySelector('h1').innerText.trim() : '',
    visible_text: document.body.innerText.substring(0, 2000),
    navigation: (() => {
        const results = [];
        const seen = new Set();
        const addLink = (text, href) => {
            text = (text || '').trim();
            if (text.length > 1 && href && href.startsWith('http') && !seen.has(href)) {
                seen.add(href);
                results.push({ text, href });
            }
        };
        document.querySelectorAll('nav a, header a, [role="navigation"] a, aside a').forEach(a => {
            addLink(a.textContent, a.href);
        });
        document.querySelectorAll('one-app-nav-bar-item-root').forEach(el => {
            const a = el.querySelector('a') || el.shadowRoot?.querySelector('a');
            const text = el.textContent || el.getAttribute('title') || '';
            const href = a ? a.href : '';
            addLink(text, href);
        });
        document.querySelectorAll(
            'a[href*="/lightning/o/"], a[href*="/lightning/page/"], ' +
            'a[href*="/lightning/r/"], [data-id] a, .slds-nav-vertical__action'
        ).forEach(a => {
            addLink(a.textContent || a.title, a.href);
        });
        document.querySelectorAll('.appLauncher a, [class*="navItem"] a, [class*="nav-item"] a').forEach(a => {
            addLink(a.textContent || a.title, a.href);
        });
        return results.slice(0, 30);
    })(),
    buttons: Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'))
                .map(b => ({ text: (b.textContent || b.value || '').trim().substring(0, 80), tag: b.tagName.toLowerCase() }))
                .filter(b => b.text.length > 2)
                .slice(0, 30),
    forms: Array.from(document.querySelectorAll('form')).length,
    inputs: Array.from(document.querySelectorAll('input, select, textarea'))
                .map(i => ({ type: i.type || i.tagName.toLowerCase(), name: i.name || '', placeholder: i.placeholder || '' }))
                .slice(0, 20),
    tables: Array.from(document.querySelectorAll('table')).length,
    modals: Array.from(document.querySelectorAll('[role="dialog"], .modal, .modal-dialog')).length
})"""


# ---------------------------------------------------------------------------
# Scrolling Support
# ---------------------------------------------------------------------------

_DEFAULT_SCROLL_DELTA = 400
_SCROLL_NETWORK_TIMEOUT_MS = 8000
_SCROLL_SETTLE_MS = 600


async def perform_scroll(page: Page, step: NavigationStep) -> bool:
    """Execute a scroll action smoothly for natural video recording.

    Supports:
      - SCROLL_INTO_VIEW: scroll a specific element into the viewport
      - SCROLL_DOWN: scroll viewport down by delta pixels (default 400)
      - SCROLL_TO_BOTTOM: scroll to the bottom of the page/container
      - SCROLL_BY: scroll by an arbitrary pixel delta

    Returns True if the scroll succeeded.
    """
    action = step.action
    delta = step.delta or _DEFAULT_SCROLL_DELTA
    locator_selector = step.locator or step.target

    try:
        if action == "SCROLL_INTO_VIEW":
            if not locator_selector:
                logger.warning("SCROLL_INTO_VIEW requires a locator/target selector.")
                return False
            # Try pierce selector for shadow DOM (Salesforce Lightning)
            loc = page.locator(f"pierce/{locator_selector}").first
            try:
                if not await loc.is_visible(timeout=3000):
                    loc = page.locator(locator_selector).first
            except Exception:
                loc = page.locator(locator_selector).first
            await loc.scroll_into_view_if_needed(timeout=5000)

        elif action == "SCROLL_DOWN":
            await page.mouse.wheel(0, delta)

        elif action == "SCROLL_TO_BOTTOM":
            await page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")

        elif action == "SCROLL_BY":
            await page.evaluate(f"window.scrollBy({{top: {delta}, behavior: 'smooth'}})")

        else:
            logger.warning(f"Unknown scroll action: {action}")
            return False

        # Smart waits: let network settle + natural pause for video smoothness
        await _post_scroll_wait(page)
        logger.info(f"Scroll executed: {action} (delta={delta}, target={locator_selector[:60] if locator_selector else 'N/A'})")
        return True

    except Exception as e:
        logger.warning(f"Scroll failed ({action}): {e}")
        # Fallback: JS smooth scroll
        try:
            await page.evaluate(f"window.scrollBy({{top: {delta}, behavior: 'smooth'}})")
            await _post_scroll_wait(page)
            return True
        except Exception as fallback_err:
            logger.warning(f"Scroll fallback also failed: {fallback_err}")
            return False


async def _post_scroll_wait(page: Page) -> None:
    """Wait for network idle + settle time after scrolling for smooth video."""
    try:
        await page.wait_for_load_state("networkidle", timeout=_SCROLL_NETWORK_TIMEOUT_MS)
    except Exception:
        pass
    await page.wait_for_timeout(_SCROLL_SETTLE_MS)


async def capture_page(page, state=None):
    """Capture page state: screenshot + DOM extraction."""
    try:
        if not await should_start_recording(page):
            return None
    except Exception:
        return None

    try:
        await asyncio.sleep(1)
    except Exception:
        pass

    await dismiss_common_popups(page)

    try:
        dom_raw = await page.evaluate(_DOM_EXTRACTION_SCRIPT)
        dom_summary = json.loads(dom_raw) if isinstance(dom_raw, str) else dom_raw
    except Exception as e:
        logger.warning(f"DOM extraction failed: {e}")
        dom_summary = {"title": "", "h1": "", "visible_text": "", "navigation": [], "buttons": [], "forms": 0, "inputs": [], "tables": 0, "modals": 0}

    screenshot_path = None
    try:
        title_slug = _safe_filename(dom_summary.get("title", "page"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = str(SCREENSHOTS_DIR / f"{ts}_{title_slug}.png")
        await page.screenshot(path=screenshot_path, full_page=False)
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")

    return PageCapture(
        url=page.url,
        title=dom_summary.get("title", ""),
        dom_summary=dom_summary,
        screenshot_path=screenshot_path,
        buttons=dom_summary.get("buttons", []),
        forms_count=dom_summary.get("forms", 0),
    )
