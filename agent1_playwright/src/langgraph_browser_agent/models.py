from pydantic import BaseModel, Field
from typing import List, Literal, Optional


# Supported navigation action types including scroll variants
ActionType = Literal[
    "click_nav",
    "click_button",
    "goto_url",
    "SCROLL_INTO_VIEW",
    "SCROLL_DOWN",
    "SCROLL_TO_BOTTOM",
    "SCROLL_BY",
]


class NavigationStep(BaseModel):
    """A single navigation or scroll action in a workflow plan."""
    action: ActionType
    target: str = ""
    description: str = ""
    wait_after: int = 2
    delta: Optional[int] = Field(default=None, description="Scroll distance in pixels (for SCROLL_BY/SCROLL_DOWN)")
    locator: Optional[str] = Field(default=None, description="CSS selector to scroll into view (for SCROLL_INTO_VIEW)")


class UIElement(BaseModel):
    element_type: str
    visible_text: str
    purpose: str
    suggested_action: str


class PageCapture(BaseModel):
    url: str
    title: str
    screenshot_path: Optional[str] = None
    dom_summary: dict
    navigation_links: List[str] = []
    buttons: List[dict] = []
    forms_count: int = 0


class PageContext(BaseModel):
    url: str
    title: str
    description: str
    key_elements: List[UIElement]
    main_actions: List[str]


class Agent1Output(BaseModel):
    platform_name: str
    pages_captured: List[PageCapture] = []
    current_page: PageContext
    overall_user_journey: str
    relevant_workflows: List[str]
    context_for_video: str = Field(
        ...,
        description="Rich narration script optimized for demo video generation",
    )
    video_clips: List[dict] = Field(default_factory=list)
    trim_start_seconds: float = Field(default=0)
