from langgraph.graph import StateGraph, END

from .state import AgentState
from .nodes import navigate_and_crawl, analyze_and_generate_output


def create_agent1_graph():
    """Create the Agent 1 workflow: navigate → analyze → END."""
    workflow = StateGraph(AgentState)

    workflow.add_node("navigate_and_crawl", navigate_and_crawl)
    workflow.add_node("analyze", analyze_and_generate_output)

    workflow.set_entry_point("navigate_and_crawl")
    workflow.add_edge("navigate_and_crawl", "analyze")
    workflow.add_edge("analyze", END)

    return workflow.compile()
