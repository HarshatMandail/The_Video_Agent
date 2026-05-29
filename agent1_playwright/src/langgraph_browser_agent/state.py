from typing import TypedDict, Optional

from .models import Agent1Output, PageCapture


class AgentState(TypedDict):
    url: str
    user_query: str
    page_captures: Optional[list[PageCapture]]
    structured_output: Optional[Agent1Output]
    task_completed: Optional[bool]
    trim_start_seconds: Optional[float]
