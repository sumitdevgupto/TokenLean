"""
Token Optimisation Proxy — Python Multi-Agent LangGraph Starter Kit (G16)

Demonstrates: typed state schema (G09), per-node tool scoping (G08),
token budget propagation (G17), and loop control.
"""
import os
from typing import Annotated, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

PROXY_ENDPOINT = os.environ["PROXY_ENDPOINT"]
PROXY_API_KEY  = os.environ["PROXY_API_KEY"]

_llm = ChatOpenAI(
    model="gpt-4o-mini",
    api_key=PROXY_API_KEY,
    base_url=f"{PROXY_ENDPOINT}/v1",
    max_tokens=512,
    max_retries=2,
)


class AgentState(TypedDict):
    """Compact typed state schema (G09) — no free-form text blobs."""
    messages: Annotated[list, add_messages]
    task: str
    result: str
    token_budget_remaining: int   # G17 budget propagation
    workflow_id: str


def router_node(state: AgentState) -> AgentState:
    """Classify task → route to specialist sub-agent."""
    response = _llm.invoke(
        [{"role": "user", "content": f"Classify this task in one word (research/write/code): {state['task']}"}],
        extra_body={
            "workflow_id": state["workflow_id"],
            "user": "langgraph-demo",
        },
    )
    return {**state, "result": response.content.strip().lower()}


def specialist_node(state: AgentState) -> AgentState:
    """Execute the task with only necessary context (G10 state externalisation)."""
    response = _llm.invoke(
        state["messages"] + [{"role": "user", "content": state["task"]}],
        extra_body={
            "workflow_id": state["workflow_id"],
            "x_json_output": True,    # G11: request JSON output
            "user": "langgraph-demo",
        },
    )
    return {**state, "result": response.content}


def should_continue(state: AgentState) -> str:
    if state.get("token_budget_remaining", 1) <= 0:
        return END
    if state.get("result"):
        return END
    return "specialist"


graph = StateGraph(AgentState)
graph.add_node("router", router_node)
graph.add_node("specialist", specialist_node)
graph.add_edge(START, "router")
graph.add_conditional_edges("router", should_continue, {"specialist": "specialist", END: END})
graph.add_edge("specialist", END)
app = graph.compile()


if __name__ == "__main__":
    result = app.invoke({
        "messages": [],
        "task": "Write a two-sentence summary of transformer architecture.",
        "result": "",
        "token_budget_remaining": 5000,
        "workflow_id": "demo-wf-001",
    })
    print(result["result"])
