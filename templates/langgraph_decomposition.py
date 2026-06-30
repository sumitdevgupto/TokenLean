"""
Production-Grade LangGraph Agent Decomposition Template

Demonstrates best practices for decomposing monolithic agents:
- Role-based node separation
- Conditional routing
- Token budget management per sub-agent
- Cost tracking across workflow

This is a blueprint for complex workflows like customer support,
legal document review, or multi-step analysis pipelines.
"""
import asyncio
from typing import Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from middleware.g17_loop_control import InterAgentState


# State schema
class AgentState(TypedDict):
    """State with token optimization tracking."""
    messages: List[Dict]
    token_opt_state: InterAgentState
    intent: Optional[str]
    entities: Optional[Dict]
    draft_response: Optional[str]
    final_response: Optional[str]
    cost_accumulated: float
    should_stop: bool


# Node implementations
def create_intent_classifier():
    """Create intent classification node."""
    async def classify(state: AgentState) -> Command:
        """Classify user intent to route to appropriate sub-agent."""
        # Simulate intent classification
        messages = state["messages"]
        last_message = messages[-1].get("content", "").lower() if messages else ""
        
        # Simple keyword-based classification (replace with LLM call)
        if any(word in last_message for word in ["refund", "return", "money back"]):
            intent = "refund_request"
        elif any(word in last_message for word in ["status", "where is", "track"]):
            intent = "order_inquiry"
        elif any(word in last_message for word in ["help", "support", "problem"]):
            intent = "technical_support"
        else:
            intent = "general_inquiry"
        
        # Update cost
        cost = state.get("cost_accumulated", 0) + 0.001  # ~200 tokens
        
        return Command(
            goto="route_by_intent",
            update={
                "intent": intent,
                "cost_accumulated": cost,
            }
        )
    
    return classify


def create_router():
    """Create conditional routing node."""
    def route_by_intent(state: AgentState) -> str:
        """Route to appropriate specialist based on intent."""
        intent = state.get("intent", "general_inquiry")
        
        routing_map = {
            "refund_request": "refund_specialist",
            "order_inquiry": "order_specialist",
            "technical_support": "tech_specialist",
            "general_inquiry": "general_specialist",
        }
        
        next_node = routing_map.get(intent, "general_specialist")
        
        # Check budget before routing
        token_state = state.get("token_opt_state")
        if token_state and token_state.token_budget_remaining < 500:
            return "budget_exhausted_handler"
        
        return next_node
    
    return route_by_intent


def create_refund_specialist():
    """Create refund processing specialist node."""
    async def process_refund(state: AgentState) -> Command:
        """Handle refund request with domain expertise."""
        # Simulate domain-specific processing
        entities = {"order_id": "12345", "amount": 99.99}
        
        # Simulate response generation
        response = (
            "I've located your order #12345. Since this is within our 30-day "
            "return window, I can process a full refund of $99.99. "
            "The refund will appear in 3-5 business days."
        )
        
        # Update cost (larger response)
        cost = state.get("cost_accumulated", 0) + 0.003
        
        return Command(
            goto="quality_checker",
            update={
                "entities": entities,
                "draft_response": response,
                "cost_accumulated": cost,
            }
        )
    
    return process_refund


def create_order_specialist():
    """Create order tracking specialist node."""
    async def track_order(state: AgentState) -> Command:
        """Handle order status inquiry."""
        # Simulate order lookup
        order_info = {"status": "shipped", "eta": "2 days"}
        
        response = (
            "Your order has been shipped and is expected to arrive in 2 days. "
            "Tracking number: 1Z999AA10123456784"
        )
        
        cost = state.get("cost_accumulated", 0) + 0.002
        
        return Command(
            goto="quality_checker",
            update={
                "entities": order_info,
                "draft_response": response,
                "cost_accumulated": cost,
            }
        )
    
    return track_order


def create_tech_specialist():
    """Create technical support specialist node."""
    async def provide_support(state: AgentState) -> Command:
        """Handle technical issues."""
        # Simulate troubleshooting
        solution = "Please try clearing your browser cache and cookies."
        
        cost = state.get("cost_accumulated", 0) + 0.0025
        
        return Command(
            goto="quality_checker",
            update={
                "draft_response": solution,
                "cost_accumulated": cost,
            }
        )
    
    return provide_support


def create_general_specialist():
    """Create general inquiry handler."""
    async def handle_general(state: AgentState) -> Command:
        """Handle general questions."""
        response = (
            "Thank you for reaching out. I'd be happy to help you with "
            "any questions about our products or services."
        )
        
        cost = state.get("cost_accumulated", 0) + 0.0015
        
        return Command(
            goto="quality_checker",
            update={
                "draft_response": response,
                "cost_accumulated": cost,
            }
        )
    
    return handle_general


def create_quality_checker():
    """Create quality assurance node."""
    async def check_quality(state: AgentState) -> Command:
        """Verify response quality before delivery."""
        draft = state.get("draft_response", "")
        
        # Simple quality checks (replace with actual QA logic)
        issues = []
        if len(draft) < 20:
            issues.append("too_short")
        if "error" in draft.lower() or "fail" in draft.lower():
            issues.append("negative_tone")
        
        cost = state.get("cost_accumulated", 0) + 0.001
        
        if issues:
            # Route back for improvement
            return Command(
                goto="improve_response",
                update={
                    "quality_issues": issues,
                    "cost_accumulated": cost,
                }
            )
        
        # Pass quality check
        return Command(
            goto="finalize",
            update={
                "final_response": draft,
                "cost_accumulated": cost,
            }
        )
    
    return check_quality


def create_response_improver():
    """Create response improvement node."""
    async def improve(state: AgentState) -> Command:
        """Improve response based on quality feedback."""
        draft = state.get("draft_response", "")
        issues = state.get("quality_issues", [])
        
        # Simulate improvement
        improved = draft
        if "too_short" in issues:
            improved += " Please let me know if you need any additional information."
        
        cost = state.get("cost_accumulated", 0) + 0.002
        
        return Command(
            goto="finalize",
            update={
                "final_response": improved,
                "cost_accumulated": cost,
            }
        )
    
    return improve


def create_budget_handler():
    """Create budget exhaustion handler."""
    async def handle_budget_exhausted(state: AgentState) -> Command:
        """Handle case where token budget is exhausted."""
        response = (
            "I apologize, but I've reached the processing limit for this request. "
            "Let me escalate this to a human agent who can provide further assistance."
        )
        
        return Command(
            goto=END,
            update={
                "final_response": response,
                "should_stop": True,
            }
        )
    
    return handle_budget_exhausted


def create_finalizer():
    """Create response finalization node."""
    async def finalize(state: AgentState) -> Command:
        """Finalize response and prepare output."""
        final = state.get("final_response", "")
        
        # Add to messages
        messages = state.get("messages", [])
        messages.append({"role": "assistant", "content": final})
        
        # Log final cost
        total_cost = state.get("cost_accumulated", 0)
        print(f"[Cost Report] Total workflow cost: ${total_cost:.4f}")
        
        return Command(
            goto=END,
            update={
                "messages": messages,
                "should_stop": True,
            }
        )
    
    return finalize


def build_customer_support_graph():
    """
    Build a complete customer support decomposition graph.
    
    This demonstrates how to break down a monolithic customer support agent
    into specialized sub-agents with conditional routing.
    """
    # Create graph
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("classify", create_intent_classifier())
    workflow.add_node("route_by_intent", lambda s: s)  # Router is a condition
    workflow.add_node("refund_specialist", create_refund_specialist())
    workflow.add_node("order_specialist", create_order_specialist())
    workflow.add_node("tech_specialist", create_tech_specialist())
    workflow.add_node("general_specialist", create_general_specialist())
    workflow.add_node("quality_checker", create_quality_checker())
    workflow.add_node("improve_response", create_response_improver())
    workflow.add_node("budget_exhausted_handler", create_budget_handler())
    workflow.add_node("finalize", create_finalizer())
    
    # Add edges
    workflow.add_edge(START, "classify")
    workflow.add_edge("classify", "route_by_intent")
    
    # Conditional routing
    workflow.add_conditional_edges(
        "route_by_intent",
        create_router(),
        {
            "refund_specialist": "refund_specialist",
            "order_specialist": "order_specialist",
            "tech_specialist": "tech_specialist",
            "general_specialist": "general_specialist",
            "budget_exhausted_handler": "budget_exhausted_handler",
        }
    )
    
    # Specialist -> Quality checker
    for specialist in ["refund_specialist", "order_specialist", "tech_specialist", "general_specialist"]:
        workflow.add_edge(specialist, "quality_checker")
    
    # Quality -> Improve or Finalize
    workflow.add_conditional_edges(
        "quality_checker",
        lambda s: "improve" if s.get("quality_issues") else "finalize",
        {
            "improve": "improve_response",
            "finalize": "finalize",
        }
    )
    
    # Improve -> Finalize
    workflow.add_edge("improve_response", "finalize")
    
    # Budget handler -> END
    workflow.add_edge("budget_exhausted_handler", END)
    workflow.add_edge("finalize", END)
    
    return workflow.compile()


# Cost modeling example
class DecompositionCostModel:
    """Cost model for decomposition vs monolithic comparison."""
    
    @staticmethod
    def analyze_workflow_efficiency():
        """
        Analyze efficiency of decomposed workflow.
        
        Demonstrates cost savings from:
        - Specialized prompts (smaller context)
        - Conditional routing (skip unnecessary steps)
        - Reusable components
        """
        # Monolithic approach: single large prompt
        mono_input_tokens = 2000  # Full context every time
        mono_output_tokens = 500
        
        # Decomposed approach: specialized contexts
        # Intent classification: small
        classify_input = 200
        classify_output = 50
        
        # Specialist: medium (domain-focused)
        specialist_input = 500
        specialist_output = 300
        
        # Quality check: small
        qa_input = 300
        qa_output = 50
        
        # Calculate costs (gpt-4o-mini pricing)
        def cost(tokens, is_input=True):
            rate = 0.00015 if is_input else 0.0006
            return (tokens / 1000) * rate
        
        mono_cost = cost(mono_input_tokens, True) + cost(mono_output_tokens, False)
        
        decomp_cost = (
            cost(classify_input, True) + cost(classify_output, False) +
            cost(specialist_input, True) + cost(specialist_output, False) +
            cost(qa_input, True) + cost(qa_output, False)
        )
        
        savings = mono_cost - decomp_cost
        
        return {
            "monolithic_cost": mono_cost,
            "decomposed_cost": decomp_cost,
            "savings_usd": savings,
            "savings_percent": (savings / mono_cost) * 100,
            "efficiency_gain": "Significant for multi-intent workflows",
        }


if __name__ == "__main__":
    # Test the graph
    async def test():
        graph = build_customer_support_graph()
        
        # Test refund request
        result = await graph.ainvoke({
            "messages": [
                {"role": "user", "content": "I want a refund for my order"}
            ],
            "token_opt_state": InterAgentState(
                token_budget_remaining=2000,
                workflow_turn=1,
                max_iterations=10,
            ),
            "intent": None,
            "entities": None,
            "draft_response": None,
            "final_response": None,
            "cost_accumulated": 0.0,
            "should_stop": False,
        })
        
        print("Final response:", result.get("final_response"))
        print("Total cost:", result.get("cost_accumulated"))
        
        # Show cost model
        efficiency = DecompositionCostModel.analyze_workflow_efficiency()
        print(f"\nEfficiency analysis:")
        print(f"  Monolithic: ${efficiency['monolithic_cost']:.4f}")
        print(f"  Decomposed: ${efficiency['decomposed_cost']:.4f}")
        print(f"  Savings: {efficiency['savings_percent']:.1f}%")
    
    asyncio.run(test())
