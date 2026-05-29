import logging

from .browser_pool import shutdown_browser_pool
from .config import validate_config, ENABLE_LANGSMITH, LANGSMITH_PROJECT
from .graph import create_agent1_graph
from .logger import setup_logging
from .models import Agent1Output
from .state import AgentState

logger = logging.getLogger(__name__)

setup_logging()


async def run_agent1(
    url: str,
    user_query: str,
    cleanup_browser: bool = False,
) -> Agent1Output:
    """Main entry point for Agent 1. Returns structured platform analysis."""
    errors = validate_config()
    if errors:
        raise ValueError(f"Configuration errors: {'; '.join(errors)}")

    config = {}
    if ENABLE_LANGSMITH:
        config["metadata"] = {"project": LANGSMITH_PROJECT, "url": url[:100]}
        logger.info(f"LangSmith tracing enabled (project: {LANGSMITH_PROJECT})")

    graph = create_agent1_graph()

    initial_state: AgentState = {
        "url": url,
        "user_query": user_query,
        "page_captures": None,
        "structured_output": None,
        "task_completed": None,
        "trim_start_seconds": 0,
    }

    try:
        final_state = await graph.ainvoke(initial_state, config=config)
        return final_state["structured_output"]
    finally:
        if cleanup_browser:
            await shutdown_browser_pool()
