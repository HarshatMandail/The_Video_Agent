import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse

from playwright.async_api import Page

from .browser_helpers import capture_page, is_classic_fallback_page, perform_scroll
from .browser_pool import get_browser_pool, shutdown_browser_pool, save_login_state
from .config import (
    AGENT_OUTPUT_DIR,
    NAVIGATION_TIMEOUT_MS,
    PAGE_LOAD_TIMEOUT_MS,
    SAFE_LIGHTNING_FALLBACK_URL,
    VIDEO_CLIPS_DIR,
    WAIT_FOR_LOGIN_TIMEOUT,
    LOGIN_CHECK_INTERVAL,
    RECORD_CURSOR_METADATA,
)
from .cursor_recorder import CursorRecorder
from .cost_tracker import get_session, reset_session, estimate_tokens
from .llm import analyze_with_llm
from .logger import AuditLogger
from .models import Agent1Output, NavigationStep, PageCapture, PageContext, UIElement
from .navigation_planner import plan_navigation
from .prompts import SYSTEM_PROMPT
from .security import assert_url_safe, SecurityError
from .state import AgentState

logger = logging.getLogger(__name__)

MAX_WORKFLOW_PAGES = int(os.getenv("MAX_WORKFLOW_PAGES", "6"))


def _clean_junk_recordings() -> None:
    """Remove any video files created by a discarded recording context."""
    from pathlib import Path
    video_dir = Path(VIDEO_CLIPS_DIR)
    if not video_dir.exists():
        return
    for ext in ("*.webm", "*.mp4"):
        for f in video_dir.glob(ext):
            try:
                f.unlink()
            except Exception:
                pass
    logger.info("Cleaned junk recordings from discarded context.")

_LOGIN_INDICATORS = ["/login", "/signin", "/sign-in", "/sso", "/oauth", "/authorize"]
_AUTH_INDICATORS = ["/verification", "/verify", "/mfa", "/two-factor", "/challenge", "emailverification", "/_ui/identity/"]
_REDIRECT_INDICATORS = ["frontdoor.jsp", "/secur/", "/_ui/identity/"]
_APP_INDICATORS = ["/lightning/", "/home", "/dashboard", "/app/", "/o/", "/one/one.app"]


def _is_login_page(url: str) -> bool:
    url_lower = url.lower()
    return any(i in url_lower for i in _LOGIN_INDICATORS)


def _is_auth_intermediate_page(url: str) -> bool:
    url_lower = url.lower()
    return any(i in url_lower for i in _AUTH_INDICATORS)


def _is_post_login_redirect(url: str) -> bool:
    return any(i in url.lower() for i in _REDIRECT_INDICATORS)


def _is_app_url(url: str) -> bool:
    url_lower = url.lower()
    if any(i in url_lower for i in _APP_INDICATORS):
        if not _is_login_page(url) and not _is_auth_intermediate_page(url):
            return True
    return False


def _get_home_url(current_url: str) -> str:
    parsed = urlparse(current_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if "lightning.force.com" in current_url or "salesforce.com" in current_url:
        return f"{base}/lightning/o/Home/home"
    return f"{base}/"


def _build_fallback_url(current_url: str) -> str:
    """Construct a safe Lightning list view URL from the current org domain."""
    parsed = urlparse(current_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/lightning/o/Contact/list"


async def _redirect_from_classic_fallback(page: Page, audit: AuditLogger) -> None:
    """Redirect away from Classic fallback page to a clean Lightning view."""
    fallback_url = SAFE_LIGHTNING_FALLBACK_URL or _build_fallback_url(page.url)
    logger.warning(
        "Detected Salesforce Classic fallback page. "
        f"Redirecting to safe Lightning page: {fallback_url}"
    )
    audit.log("classic_fallback_redirect", {"from": page.url[:120], "to": fallback_url})
    await page.goto(fallback_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    await _wait_for_network_idle(page, timeout_ms=10000)


async def _activate_home_tab(page: Page) -> None:
    selectors = [
        ('[data-id="Home"]', None),
        ('a[title="Home"]', None),
    ]
    for selector, _ in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=3000):
                await loc.click(timeout=5000)
                await asyncio.sleep(3)
                return
        except Exception:
            pass

    try:
        home_link = page.get_by_role("link", name="Home", exact=True)
        if await home_link.first.is_visible(timeout=3000):
            await home_link.first.click(timeout=5000)
            await asyncio.sleep(3)
            return
    except Exception:
        pass

    await asyncio.sleep(2)


async def _wait_for_login(page: Page, audit: AuditLogger) -> bool:
    logger.info("Waiting for login completion...")
    audit.log("waiting_for_login")

    elapsed = 0
    last_url = ""
    stable_app_url_count = 0

    while elapsed < WAIT_FOR_LOGIN_TIMEOUT:
        await asyncio.sleep(LOGIN_CHECK_INTERVAL)
        elapsed += LOGIN_CHECK_INTERVAL

        try:
            current_url = page.url
        except Exception:
            continue

        if current_url in ("about:blank", "", "chrome://newtab/"):
            continue

        if current_url != last_url:
            logger.info(f"URL changed: {current_url[:80]}")
            audit.log("url_change", {"url": current_url[:120]})
            last_url = current_url
            stable_app_url_count = 0

        if _is_app_url(current_url):
            stable_app_url_count += 1
            if stable_app_url_count >= 2:
                logger.info(f"Login complete: {current_url[:80]}")
                audit.log("login_complete", {"url": current_url})
                await _wait_for_network_idle(page)
                try:
                    await save_login_state(page.context)
                except Exception:
                    pass
                return True
            continue

        if _is_login_page(current_url) or _is_auth_intermediate_page(current_url):
            continue
        if _is_post_login_redirect(current_url):
            continue

    logger.error("Login timeout reached. Proceeding with current page.")
    audit.log("login_timeout")
    return False


async def _wait_for_network_idle(page: Page, timeout_ms: int = 10000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        await asyncio.sleep(2)


async def _wait_for_page_ready(page: Page) -> None:
    await asyncio.sleep(3)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


async def _scroll_element_into_view(page: Page, locator) -> None:
    try:
        await locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass


_NOISE_BUTTONS = {
    "skip", "dismiss", "got it", "no thanks", "close", "not now",
    "buy now", "buy starter", "sign up", "see terms", "learn more",
    "save 70%", "upgrade", "try free", "start trial",
}


def _filter_dom_for_llm(dom_summary: dict, is_primary_page: bool = False) -> dict:
    text_limit = 800 if is_primary_page else 300

    filtered = {
        "title": dom_summary.get("title", ""),
        "h1": dom_summary.get("h1", ""),
        "visible_text": dom_summary.get("visible_text", "")[:text_limit],
    }

    nav = dom_summary.get("navigation", [])
    seen_texts: set[str] = set()
    filtered_nav = []
    for item in nav:
        text = item.get("text", "").strip().lower()
        if text and text not in seen_texts and len(text) > 2:
            seen_texts.add(text)
            filtered_nav.append({"text": item["text"], "href": item.get("href", "")})
    nav_limit = 20 if is_primary_page else 8
    filtered["navigation"] = filtered_nav[:nav_limit]

    buttons = dom_summary.get("buttons", [])
    btn_limit = 25 if is_primary_page else 10
    filtered["buttons"] = [
        b for b in buttons[:btn_limit]
        if b.get("text", "").strip().lower() not in _NOISE_BUTTONS
    ]

    if is_primary_page:
        filtered["inputs"] = dom_summary.get("inputs", [])[:15]

    filtered["forms"] = dom_summary.get("forms", 0)
    filtered["tables"] = dom_summary.get("tables", 0)
    filtered["modals"] = dom_summary.get("modals", 0)

    return filtered


async def navigate_and_crawl(state: AgentState) -> dict:
    """Navigate to URL, handle login, record video, and capture pages."""
    url = state["url"]
    session_id = str(int(time.time()))
    audit = AuditLogger(session_id)

    logger.info(f"Starting page analysis: {url}")
    audit.log("session_start", {"url": url, "query": state["user_query"]})

    try:
        assert_url_safe(url)
    except SecurityError as e:
        logger.error(f"Security check failed: {e}")
        audit.log("security_blocked", {"url": url, "error": str(e)})
        audit.save()
        return {"page_captures": []}

    reset_session()
    # Clear LLM cache from previous runs to prevent stale responses
    # (e.g. planner returning old needs_navigation=false after prompt changes)
    from .cache import clear_cache
    clear_cache()

    pool = get_browser_pool()
    cursor_recorder = None

    try:
        context, page = await pool.acquire()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await asyncio.sleep(4)
            logger.info(f"Page loaded: {page.url}")
            audit.log("page_loaded", {"url": page.url})
        except Exception as e:
            logger.warning(f"Initial navigation issue: {e}")
            audit.log("navigation_error", {"error": str(e)})

        current_url = page.url
        if _is_app_url(current_url):
            logger.info("Already logged in, skipping login wait.")
            audit.log("already_logged_in")
        else:
            logger.info("Login page detected. Please complete login + 2FA in the browser.")
            await _wait_for_login(page, audit)
            current_url = page.url  # Update after login — URL has changed

        # Start recording in the SAME context that already has a valid session.
        # This avoids the session-loss problem caused by creating a new context.
        context, page = await pool.restart_with_recording()

        # Navigate to the Lightning shell home page (/lightning/page/home).
        # IMPORTANT: Do NOT use /lightning/o/Home/home — that triggers a
        # Visualforce override in many orgs (Developer Edition, trial orgs)
        # causing "You can't view this item in Lightning Experience".
        parsed = urlparse(current_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        target_url = f"{base}/lightning/page/home"
        await page.goto(target_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        await _wait_for_network_idle(page, timeout_ms=10000)

        # After context switch, Salesforce may invalidate the session and
        # redirect to login. If so, close the recording context (it captured
        # junk), re-authenticate in a non-recording context, then start fresh.
        post_nav_url = page.url
        if _is_login_page(post_nav_url) or _is_auth_intermediate_page(post_nav_url) or not _is_app_url(post_nav_url):
            logger.warning("Session lost after context switch — re-authenticating...")
            audit.log("session_lost_relogin", {"url": post_nav_url[:120]})

            # Discard the bad recording context and its junk video
            await shutdown_browser_pool()
            await asyncio.sleep(1)
            _clean_junk_recordings()
            pool = get_browser_pool()
            context, page = await pool.acquire()

            # Navigate to login and wait for user to re-authenticate
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await asyncio.sleep(2)
            await _wait_for_login(page, audit)

            # Now restart with a clean recording context
            context, page = await pool.restart_with_recording()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await _wait_for_network_idle(page, timeout_ms=10000)

        # If we landed on the classic fallback despite using /lightning/page/home,
        # redirect to a safe list view page.
        if await is_classic_fallback_page(page):
            fallback_url = SAFE_LIGHTNING_FALLBACK_URL or _build_fallback_url(current_url)
            await page.goto(fallback_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await _wait_for_network_idle(page, timeout_ms=10000)
            audit.log("classic_fallback_redirect", {"to": fallback_url})

        logger.info("Recording context ready. Starting task capture.")
        await asyncio.sleep(3)

        if RECORD_CURSOR_METADATA:
            cursor_recorder = CursorRecorder()
            await cursor_recorder.attach(page)

        captures = []

        primary_capture = await capture_page(page, 1)
        if primary_capture:
            captures.append(primary_capture)
            audit.log("primary_page_captured", {"url": primary_capture.url, "title": primary_capture.title})

        if primary_capture:
            workflow_captures = await _execute_workflow_navigation(
                page, primary_capture, state["user_query"], audit, state,
            )
            captures.extend(workflow_captures)

    except Exception as e:
        logger.error(f"Browser operation failed: {e}")
        audit.log("browser_error", {"error": str(e)})
        captures = []

    if cursor_recorder:
        cursor_recorder.save()

    # Stop recording immediately after capture is done.
    # This prevents the video from running during LLM analysis.
    await shutdown_browser_pool()
    await asyncio.sleep(2)
    logger.info("Browser closed — recording finalized.")

    audit.log("capture_complete", {"pages_captured": len(captures)})
    audit.save()

    return {"page_captures": captures, "trim_start_seconds": 0}


async def _execute_workflow_navigation(
    page: Page,
    primary: PageCapture,
    user_query: str,
    audit: AuditLogger,
    state=None,
) -> list[PageCapture]:
    """Navigate through workflow steps on the same page for continuous recording."""
    extra_captures = []

    try:
        plan = await plan_navigation(
            user_query=user_query,
            page_title=primary.title,
            page_url=primary.url,
            dom_summary=primary.dom_summary,
        )
    except Exception as e:
        logger.warning(f"Navigation planner failed: {e}")
        return extra_captures

    if not plan.get("needs_navigation"):
        # Safety net: if the query involves creating a record and we're on a list
        # view, the planner should have planned click_button→"New". If it didn't,
        # inject the step ourselves.
        query_lower = user_query.lower()
        creation_keywords = ["create", "new", "add"]
        is_creation_task = any(k in query_lower for k in creation_keywords)
        is_list_view = "/list" in primary.url or "list view" in primary.title.lower()

        if is_creation_task and is_list_view:
            logger.info("Planner skipped New button — injecting click_button step.")
            plan = {
                "needs_navigation": True,
                "steps": [{"action": "click_button", "target": "New", "description": "Click New to open creation form", "wait_after": 2}],
            }
        else:
            logger.info("No additional navigation needed.")
            return extra_captures

    steps = plan.get("steps", [])
    max_steps = min(len(steps), MAX_WORKFLOW_PAGES - 1)
    logger.info(f"Executing {max_steps} navigation steps...")

    for i, step in enumerate(steps[:max_steps]):
        action = step.get("action", "")
        target = step.get("target") or ""
        description = step.get("description", "")

        logger.info(f"  Step {i+1}/{max_steps}: {description} [{action}: {target[:60]}]")
        audit.log("workflow_step", {"step": i+1, "action": action, "target": target[:80]})

        try:
            await asyncio.sleep(2)

            navigated = await _execute_single_step(page, action, target, state, step_data=step)
            if not navigated:
                logger.warning(f"  Step {i+1}: Could not execute, skipping.")
                audit.log("workflow_step_skipped", {"step": i+1, "reason": "element_not_found"})
                continue

            await _wait_for_page_ready(page)
            await _wait_for_network_idle(page, timeout_ms=10000)

            if action == "click_button":
                await _wait_for_new_content(page)

            cap = await capture_page(page, len(extra_captures) + 2)
            if cap:
                extra_captures.append(cap)
                audit.log("workflow_page_captured", {"step": i+1, "url": cap.url, "title": cap.title})
                logger.info(f"  Step {i+1}: Captured -> {cap.title}")

        except Exception as e:
            logger.warning(f"  Step {i+1}: Failed - {e}")
            audit.log("workflow_step_error", {"step": i+1, "error": str(e)})
            continue

    logger.info(f"Navigation complete: {len(extra_captures)} additional pages captured.")
    return extra_captures


_SCROLL_ACTIONS = {"SCROLL_INTO_VIEW", "SCROLL_DOWN", "SCROLL_TO_BOTTOM", "SCROLL_BY"}


async def _execute_single_step(page: Page, action: str, target: str, state=None, step_data: dict | None = None) -> bool:
    if action == "goto_url":
        await page.goto(target, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        return True
    if action == "click_nav":
        return await _click_element(page, target, element_type="link")
    if action == "click_button":
        return await _click_element(page, target, element_type="button")
    if action in _SCROLL_ACTIONS:
        nav_step = NavigationStep(
            action=action,
            target=target,
            delta=step_data.get("delta") if step_data else None,
            locator=step_data.get("locator") if step_data else None,
        )
        return await perform_scroll(page, nav_step)
    logger.warning(f"Unknown action type: {action}")
    return False


async def _click_element(page: Page, text: str, element_type: str = "link") -> bool:
    text_clean = text.strip()
    url_before = page.url

    # Strategy 1: Role-based selectors
    try:
        if element_type == "link":
            locator = page.get_by_role("link", name=text_clean, exact=False)
        else:
            locator = page.get_by_role("button", name=text_clean, exact=False)

        if await locator.first.is_visible(timeout=3000):
            await _scroll_element_into_view(page, locator.first)
            await locator.first.click(timeout=5000)
            await _wait_for_navigation_or_modal(page, url_before)
            return True
    except Exception:
        pass

    # Strategy 2: Text-based CSS selectors
    if element_type == "link":
        selectors = [
            f'a:has-text("{text_clean}")',
            f'[role="link"]:has-text("{text_clean}")',
            f'one-app-nav-bar-item-root:has-text("{text_clean}")',
        ]
    else:
        selectors = [
            f'button:has-text("{text_clean}")',
            f'[role="button"]:has-text("{text_clean}")',
            f'input[value="{text_clean}"]',
        ]

    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=2000):
                await _scroll_element_into_view(page, locator)
                await locator.click(timeout=5000)
                await _wait_for_navigation_or_modal(page, url_before)
                return True
        except Exception:
            continue

    # Strategy 3: JS fallback for dynamic elements
    escaped_text = text_clean.replace('"', '\\"')
    clicked = await page.evaluate("""(targetText) => {
        const target = targetText.toLowerCase();
        const elements = document.querySelectorAll(
            'a, button, [role="button"], [role="link"], [role="tab"], one-app-nav-bar-item-root'
        );
        for (const el of elements) {
            const elText = (el.textContent || el.getAttribute('title') || '').trim().toLowerCase();
            if (elText === target || elText.includes(target)) {
                el.click();
                return true;
            }
        }
        return false;
    }""", escaped_text)

    if clicked:
        await _wait_for_navigation_or_modal(page, url_before)
        return True

    return False


async def _wait_for_navigation_or_modal(page: Page, url_before: str) -> None:
    await asyncio.sleep(1)

    if page.url != url_before:
        await _wait_for_page_ready(page)
        return

    modal_selectors = [
        '[role="dialog"]', '.slds-modal', '.forceModalContainer',
        '.uiModal', 'section[role="dialog"]',
    ]

    for _ in range(10):
        for selector in modal_selectors:
            try:
                if await page.locator(selector).first.is_visible(timeout=200):
                    await asyncio.sleep(1.5)
                    return
            except Exception:
                continue
        await asyncio.sleep(0.5)

    await asyncio.sleep(1)


async def _wait_for_new_content(page: Page) -> None:
    content_selectors = [
        '[role="dialog"]', '.slds-modal__content', '.slds-modal',
        'section[role="dialog"]', '.forceModalContainer', '.uiModal',
        'records-lwc-record-layout',
    ]

    for _ in range(12):
        for selector in content_selectors:
            try:
                if await page.locator(selector).first.is_visible(timeout=200):
                    try:
                        await page.locator(
                            '[role="dialog"] input, .slds-modal input'
                        ).first.wait_for(state="visible", timeout=4000)
                    except Exception:
                        await asyncio.sleep(1)
                    return
            except Exception:
                continue
        await asyncio.sleep(0.5)

    await asyncio.sleep(1)


async def verify_task_completion(state: AgentState) -> dict:
    """Verify the final task was completed by checking expected UI state."""
    captures = state.get("page_captures") or []
    user_query = state["user_query"]

    if not captures:
        logger.warning("[Verify] No captures — cannot verify task completion.")
        return {"task_completed": False}

    last_capture = captures[-1]
    dom = last_capture.dom_summary

    query_lower = user_query.lower()
    has_form = dom.get("forms", 0) > 0 or dom.get("modals", 0) > 0
    has_relevant_content = any(
        keyword in (dom.get("title", "") + dom.get("h1", "") + dom.get("visible_text", "")).lower()
        for keyword in _extract_keywords(query_lower)
    )

    if has_form or has_relevant_content:
        logger.info("[Verify] Task completion confirmed.")
        return {"task_completed": True}

    logger.info("[Verify] Target content not confirmed. Attempting retry...")

    try:
        pool = get_browser_pool()
        _, page = await pool.acquire()

        final_action_keywords = ["new", "create", "add", "open", "start"]
        for keyword in final_action_keywords:
            if keyword in query_lower:
                try:
                    btn = page.get_by_role("button", name=keyword, exact=False)
                    if await btn.first.is_visible(timeout=3000):
                        await btn.first.click(timeout=5000)
                        await _wait_for_new_content(page)
                        logger.info(f"[Verify] Retry click on '{keyword}' succeeded.")
                        return {"task_completed": True}
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"[Verify] Retry failed: {e}")

    logger.info("[Verify] Could not confirm task completion.")
    return {"task_completed": False}


def _extract_keywords(query: str) -> list[str]:
    stop_words = {"how", "do", "i", "a", "the", "in", "to", "for", "my", "is", "at", "on"}
    words = query.split()
    return [w for w in words if w not in stop_words and len(w) > 2][:5]


async def analyze_and_generate_output(state: AgentState) -> dict:
    """Analyze captured pages with LLM and produce structured guidance."""
    captures = state["page_captures"] or []
    user_query = state["user_query"]

    if not captures:
        logger.warning("No pages captured — returning empty output")
        return {
            "structured_output": Agent1Output(
                platform_name="Unknown",
                pages_captured=[],
                current_page=PageContext(
                    url="", title="", description="No pages were captured.",
                    key_elements=[], main_actions=[],
                ),
                overall_user_journey="No data available.",
                relevant_workflows=[],
                context_for_video="",
                video_clips=[],
            )
        }

    primary = captures[0]
    pages_data = []

    for i, cap in enumerate(captures):
        is_primary = (i == 0)
        page_role = (
            "CURRENT_PAGE (user is looking at this right now)"
            if is_primary
            else f"WORKFLOW_STEP_{i} (navigated to during workflow)"
        )
        pages_data.append({
            "page_role": page_role,
            "url": cap.url,
            "title": cap.title,
            "dom": _filter_dom_for_llm(cap.dom_summary, is_primary_page=is_primary),
            "buttons_count": len(cap.buttons),
            "forms_count": cap.forms_count,
        })

    pages_json = json.dumps(pages_data, separators=(",", ":"), default=str)
    est_tokens = estimate_tokens(pages_json)
    logger.info(f"Input to LLM: ~{est_tokens} tokens for {len(captures)} pages")

    multi_page_note = ""
    if len(captures) > 1:
        multi_page_note = (
            f"\n\n## Navigation Context\n"
            f"The system navigated through {len(captures)} pages to capture the full workflow. "
            f"Use ALL captured pages to provide accurate, detailed step-by-step guidance. "
            f"Each WORKFLOW_STEP page represents what the user will see after performing that action.\n"
        )

    user_message = (
        f"## User's Question\n"
        f"\"{user_query}\"\n\n"
        f"## Current Screen\n"
        f"The user is currently on: \"{primary.title}\" ({primary.url})\n"
        f"{multi_page_note}\n"
        f"## Page Data ({len(captures)} pages captured)\n"
        f"{pages_json}\n\n"
        f"## Instructions\n"
        f"Answer the user's question with step-by-step guidance starting from their CURRENT page.\n"
        f"The context_for_video field must be a complete narration script (200-500 words) "
        f"that starts with 'You are currently on the {primary.title} page...' and walks through "
        f"every click and screen transition needed to complete the task.\n"
        f"Use the actual page data from each workflow step to describe what the user will see "
        f"at each stage — real button names, real form fields, real page titles.\n"
        f"Respond with JSON matching the schema in your system instructions."
    )

    use_mini = len(captures) == 1 and est_tokens < 2000

    try:
        raw = await analyze_with_llm(SYSTEM_PROMPT, user_message, use_mini=use_mini)
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response: {e}")
        data = _fallback_output(captures, raw if "raw" in dir() else "")
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        data = _fallback_output(captures, str(e))

    current_page = PageContext(
        url=data["current_page"]["url"],
        title=data["current_page"]["title"],
        description=data["current_page"]["description"],
        key_elements=[UIElement(**el) for el in data["current_page"].get("key_elements", [])],
        main_actions=data["current_page"].get("main_actions", []),
    )

    workflows = _normalize_workflows(data.get("relevant_workflows", []))

    output = Agent1Output(
        platform_name=data["platform_name"],
        pages_captured=captures,
        current_page=current_page,
        overall_user_journey=data["overall_user_journey"],
        relevant_workflows=workflows,
        context_for_video=data.get("context_for_video", ""),
        video_clips=[],
        trim_start_seconds=state.get("trim_start_seconds", 0),
    )

    session = get_session()
    summary = session.get_summary()
    logger.info(
        f"Session cost: ${summary['total_cost_usd']} | "
        f"Calls: {summary['call_count']} | Cache hits: {summary['cache_hits']}"
    )

    _save_agent_output(output)

    return {"structured_output": output}


def _save_agent_output(output: Agent1Output) -> None:
    timestamp = int(time.time())
    output_file = AGENT_OUTPUT_DIR / f"agent1_output_{timestamp}.json"

    data = output.model_dump()
    for page in data.get("pages_captured", []):
        page.pop("dom_summary", None)
        page.pop("screenshot_path", None)

    output_file.write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info(f"Agent 1 output saved: {output_file}")


def _fallback_output(captures: list[PageCapture], error_detail: str) -> dict:
    return {
        "platform_name": "Unknown",
        "current_page": {
            "url": captures[0].url if captures else "",
            "title": captures[0].title if captures else "",
            "description": f"Analysis failed: {error_detail[:200]}",
            "key_elements": [],
            "main_actions": [],
        },
        "overall_user_journey": "Unable to determine.",
        "relevant_workflows": [],
        "context_for_video": "",
    }


def _normalize_workflows(raw_workflows: list) -> list[str]:
    workflows = []
    for item in raw_workflows:
        if isinstance(item, str):
            workflows.append(item)
        elif isinstance(item, dict):
            name = item.get("workflow_name", item.get("name", ""))
            steps = item.get("steps", item.get("description", ""))
            if isinstance(steps, list):
                steps = " -> ".join(str(s) for s in steps)
            workflows.append(f"{name}: {steps}" if name else str(steps))
        else:
            workflows.append(str(item))
    return workflows
