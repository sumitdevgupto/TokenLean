"""
LangGraph Node Template with x-token-opt-state Header Support

This template demonstrates how to create a LangGraph node that:
1. Reads the x-token-opt-state header from upstream agents
2. Respects token_budget_remaining from the header
3. Passes updated state to downstream agents

Usage:
    from langgraph_node_template import create_token_aware_node
    
    # Create a node that respects token budgets
    node = create_token_aware_node("my_node", my_node_logic)
    
    # Add to your graph
    graph.add_node("my_node", node)
"""
import base64
import json
from typing import Any, Callable, Dict, Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command


class TokenOptState(TypedDict):
    """Token optimization state from x-token-opt-state header."""
    token_budget_remaining: int
    workflow_turn: int
    max_iterations: int
    confidence_score: Optional[float]
    wall_clock_elapsed_seconds: Optional[float]
    stop_reason: Optional[str]


class AgentState(TypedDict):
    """Example agent state with token optimization support."""
    messages: list
    token_opt_state: Optional[TokenOptState]
    should_stop: bool


def parse_token_opt_header(header_value: str) -> TokenOptState:
    """Parse x-token-opt-state header value.
    
    The header value is base64-encoded JSON.
    """
    json_bytes = base64.b64decode(header_value)
    data = json.loads(json_bytes.decode("utf-8"))
    return TokenOptState(**data)


def serialize_token_opt_state(state: TokenOptState) -> str:
    """Serialize TokenOptState to header format."""
    data = dict(state)
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(json_bytes).decode("utf-8")


def create_token_aware_node(
    name: str,
    logic: Callable[[AgentState, TokenOptState], Dict[str, Any]]
) -> Callable[[AgentState], Command]:
    """Create a LangGraph node that respects token budgets.
    
    Args:
        name: Node name for logging/debugging
        logic: Function that takes (state, token_opt_state) and returns updates
        
    Returns:
        LangGraph-compatible node function
    """
    def node(state: AgentState) -> Command:
        # Extract token optimization state
        token_opt = state.get("token_opt_state")
        
        # Check if we should stop due to budget constraints
        if token_opt:
            remaining = token_opt.get("token_budget_remaining", 0)
            workflow_turn = token_opt.get("workflow_turn", 1)
            max_iterations = token_opt.get("max_iterations", 5)
            confidence = token_opt.get("confidence_score")
            
            # Stop conditions
            if remaining <= 0:
                print(f"[{name}] Stopping: token budget exhausted ({remaining} remaining)")
                return Command(
                    goto=END,
                    update={"should_stop": True, "stop_reason": "token_budget_exhausted"}
                )
            
            if workflow_turn >= max_iterations:
                print(f"[{name}] Stopping: max iterations reached ({workflow_turn}/{max_iterations})")
                return Command(
                    goto=END,
                    update={"should_stop": True, "stop_reason": "max_iterations_reached"}
                )
            
            if confidence and confidence >= 0.95:
                print(f"[{name}] Stopping: high confidence achieved ({confidence:.2f})")
                return Command(
                    goto=END,
                    update={"should_stop": True, "stop_reason": "high_confidence"}
                )
        
        # Execute node logic
        try:
            result = logic(state, token_opt or TokenOptState(
                token_budget_remaining=4000,
                workflow_turn=1,
                max_iterations=5,
            ))
            
            # Update token optimization state for next node
            if token_opt:
                new_token_opt = TokenOptState(
                    token_budget_remaining=token_opt["token_budget_remaining"] - result.get("tokens_used", 0),
                    workflow_turn=token_opt["workflow_turn"] + 1,
                    max_iterations=token_opt["max_iterations"],
                    confidence_score=result.get("confidence", token_opt.get("confidence_score")),
                    wall_clock_elapsed_seconds=None,  # Updated by orchestrator
                    stop_reason=None,
                )
            else:
                new_token_opt = None
            
            return Command(
                goto=result.get("next", END),
                update={
                    "messages": result.get("messages", state["messages"]),
                    "token_opt_state": new_token_opt,
                    **result.get("extra_updates", {})
                }
            )
            
        except Exception as exc:
            print(f"[{name}] Error in node logic: {exc}")
            return Command(
                goto=END,
                update={"should_stop": True, "stop_reason": f"error: {exc}"}
            )
    
    return node


# Example usage
if __name__ == "__main__":
    # Define example node logic
    def example_node_logic(state: AgentState, token_opt: TokenOptState) -> Dict[str, Any]:
        """Example node that processes messages and returns updates."""
        # Simulate token usage
        tokens_used = 100
        confidence = 0.85
        
        return {
            "next": "next_node",  # Or END to stop
            "messages": state["messages"] + [{"role": "assistant", "content": "Processed"}],
            "tokens_used": tokens_used,
            "confidence": confidence,
            "extra_updates": {"processed": True}
        }
    
    # Create the graph
    workflow = StateGraph(AgentState)
    
    # Create token-aware nodes
    start_node = create_token_aware_node("start", example_node_logic)
    
    # Add nodes
    workflow.add_node("start", start_node)
    
    # Set entry point
    workflow.set_entry_point("start")
    
    # Compile
    app = workflow.compile()
    
    # Example invocation with token optimization state
    result = app.invoke({
        "messages": [{"role": "user", "content": "Hello"}],
        "token_opt_state": {
            "token_budget_remaining": 1000,
            "workflow_turn": 1,
            "max_iterations": 3,
            "confidence_score": None,
            "wall_clock_elapsed_seconds": None,
            "stop_reason": None,
        },
        "should_stop": False,
    })
    
    print("Result:", result)
