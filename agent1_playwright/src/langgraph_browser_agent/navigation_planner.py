import json
import logging
from typing import Any

from .llm import analyze_with_llm

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """\
You are a navigation planner for a SaaS platform tutorial system.
You are also an EXPERT on Salesforce Lightning, CRM platforms, and SaaS applications.

Given the user's question and the current page DOM data, decide what navigation steps are needed to capture the full workflow.

## CRITICAL — Two Planning Modes:

### Mode 1: DOM-Based (target IS visible in the DOM)
If the target page/feature is directly visible in the navigation links or buttons,
plan clicks on those visible elements.

### Mode 2: Knowledge-Based (target is NOT visible in the DOM)
If the target page/feature is NOT in the navigation links or buttons provided,
use your PLATFORM KNOWLEDGE to plan the full click path. Do NOT say "no navigation needed"
just because the target isn't visible. Instead, plan every intermediate click needed
to reach it (opening menus, dropdowns, App Launcher, "View All", etc.).

Examples of knowledge-based planning:
- Target is in App Launcher: click_button → "App Launcher" → click_button → "View All" → click_nav → "[Target]"
- Target is under "More" dropdown: click_button → "More" → click_nav → "[Target]"
- Target needs search: click_button → "Search..." → (type query) → click result

## Rules:
1. If the user's question can be answered from the CURRENT page alone (no navigation needed), return needs_navigation=false.
2. If the task requires navigating to other pages, plan the navigation steps.
3. Each step should describe ONE click/navigation/scroll action with a CSS selector or link text.
4. Maximum 6 navigation steps.
5. Use the DOM data (navigation links, buttons) to identify elements when possible.
6. When the target is NOT in the DOM, use your knowledge of the platform to plan the path.
7. Prefer clicking navigation links/buttons over typing URLs.
8. If a step requires opening a form (like "New"), include that as a step.
9. **SCROLLING**: Use scroll actions when:
   - The target element might be below the fold (long forms, tables, sections)
   - The page has "View More", "Load More", or paginated tables
   - A CRM form has many fields that extend beyond the viewport
   Available scroll actions:
   - "SCROLL_INTO_VIEW": Scroll a specific element into the viewport. Set "locator" to a CSS selector.
   - "SCROLL_DOWN": Scroll the page down by "delta" pixels (default 400).
   - "SCROLL_TO_BOTTOM": Scroll to the very bottom of the page.
   - "SCROLL_BY": Scroll by a custom pixel amount set in "delta".

## Salesforce-Specific Navigation Knowledge:

### App Launcher (9-dot grid icon in top-left):
Use this when the target app/object is NOT in the top navigation bar.
  Step 1: click_button → "App Launcher" (opens the launcher panel)
  Step 2: click_button → "View All" (shows the full list of apps/items)
  Step 3: click_nav → "[Target App Name]" (click the target)

### Record Creation (Contact, Lead, Opportunity, Account, Case, etc.):
For ANY task that involves creating, adding, or opening a new record:

Scenario A — User is NOT on the object's list view:
  Step 1: click_nav → "Contacts" (navigate to the list view)
  Step 2: click_button → "New" (open the creation form)

Scenario B — User is ALREADY on the object's list view:
  Step 1: click_button → "New" (open the creation form)

RULES for record creation:
- ALWAYS set needs_navigation=true for record creation tasks.
- ALWAYS include click_button → "New" as a step.
- NEVER return needs_navigation=false for record creation tasks.
- NEVER use the Global Actions "+" (quick action) menu in the top-right corner.

### "More" Dropdown:
If a tab is not visible in the nav bar but exists in the org:
  Step 1: click_button → "More" (opens overflow dropdown)
  Step 2: click_nav → "[Target Tab]"

## Output Format (strict JSON):
{
  "needs_navigation": true/false,
  "reasoning": "Brief explanation",
  "steps": [
    {
      "action": "click_nav" | "click_button" | "goto_url" | "SCROLL_INTO_VIEW" | "SCROLL_DOWN" | "SCROLL_TO_BOTTOM" | "SCROLL_BY",
      "target": "exact text of link/button OR url OR CSS selector for scroll target",
      "description": "What this step does",
      "wait_after": 2,
      "delta": null,
      "locator": null
    }
  ]
}

Notes on delta/locator:
- "delta" is only used for SCROLL_DOWN and SCROLL_BY (integer, pixels).
- "locator" is only used for SCROLL_INTO_VIEW (CSS selector string).
- For click actions, leave delta and locator as null.
"""


def _build_planner_input(
    user_query: str,
    page_title: str,
    page_url: str,
    dom_summary: dict,
) -> str:
    nav_links = dom_summary.get("navigation", [])
    buttons = dom_summary.get("buttons", [])
    visible_text = dom_summary.get("visible_text", "")[:500]

    return (
        f"## User Query\n\"{user_query}\"\n\n"
        f"## Current Page\n"
        f"Title: {page_title}\n"
        f"URL: {page_url}\n\n"
        f"## Available Navigation Links\n"
        f"{json.dumps(nav_links[:25], default=str)}\n\n"
        f"## Available Buttons\n"
        f"{json.dumps(buttons[:20], default=str)}\n\n"
        f"## Visible Text (first 500 chars)\n"
        f"{visible_text}\n\n"
        f"## Instructions\n"
        f"Based on the user's question and what's available on this page, "
        f"plan the navigation steps needed to capture the full workflow. "
        f"If the current page already has everything needed, return needs_navigation=false."
    )


async def plan_navigation(
    user_query: str,
    page_title: str,
    page_url: str,
    dom_summary: dict,
) -> dict[str, Any]:
    """Ask the LLM to plan navigation steps based on user query and current page."""
    user_message = _build_planner_input(user_query, page_title, page_url, dom_summary)

    logger.info("Analyzing page for required navigation...")

    raw = await analyze_with_llm(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_message=user_message,
        use_mini=True,
    )

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse planner response, defaulting to no navigation")
        return {"needs_navigation": False, "reasoning": "Parse error", "steps": []}

    needs_nav = result.get("needs_navigation", False)
    steps = result.get("steps", [])

    logger.info(
        f"Navigation plan: needs_navigation={needs_nav} | "
        f"steps={len(steps)} | reason={result.get('reasoning', '')[:80]}"
    )

    return result
