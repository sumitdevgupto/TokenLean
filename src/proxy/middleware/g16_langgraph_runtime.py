"""
G16 LangGraph Runtime — Agent Architecture Orchestration

Provides LangGraph runtime integration for agent workflows:
1. Conditional edge routing based on state
2. Sub-agent spawning with budget tracking
3. Cost modeling integration
4. Async execution framework

Uses LangGraph for production-grade agent decomposition.
"""
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command

from middleware import RequestContext
from middleware.g17_loop_control import InterAgentState

logger = logging.getLogger(__name__)
GROUP = "G16_LANGGRAPH"


class LangGraphAgentState(TypedDict):
    """State schema for LangGraph agents with token optimization."""
    messages: List[Dict]
    token_opt_state: Optional[InterAgentState]
    sub_agents: List[str]
    cost_accumulated_usd: float
    should_stop: bool
    next_node: Optional[str]


class LangGraphRuntime:
    """LangGraph runtime for agent workflows."""
    
    def __init__(self):
        self._graphs: Dict[str, StateGraph] = {}
        self._node_handlers: Dict[str, Callable] = {}
    
    def create_graph(self, name: str) -> StateGraph:
        """Create a new agent graph."""
        graph = StateGraph(LangGraphAgentState)
        self._graphs[name] = graph
        return graph
    
    def add_node(
        self,
        graph_name: str,
        node_name: str,
        handler: Callable[[LangGraphAgentState], Command],
    ):
        """Add a node to a graph."""
        if graph_name not in self._graphs:
            raise ValueError(f"Graph {graph_name} not found")
        
        self._graphs[graph_name].add_node(node_name, handler)
        self._node_handlers[f"{graph_name}:{node_name}"] = handler
    
    def add_conditional_edges(
        self,
        graph_name: str,
        source: str,
        condition: Callable[[LangGraphAgentState], str],
        targets: Dict[str, str],
    ):
        """Add conditional routing edges."""
        if graph_name not in self._graphs:
            raise ValueError(f"Graph {graph_name} not found")
        
        self._graphs[graph_name].add_conditional_edges(
            source,
            condition,
            targets,
        )
    
    def compile_graph(self, graph_name: str):
        """Compile graph for execution."""
        if graph_name not in self._graphs:
            raise ValueError(f"Graph {graph_name} not found")
        
        return self._graphs[graph_name].compile()
    
    async def execute(
        self,
        graph_name: str,
        initial_state: LangGraphAgentState,
        max_iterations: int = 10,
    ) -> LangGraphAgentState:
        """
        Execute a graph with budget tracking.
        
        Respects token_budget_remaining and stops if exhausted.
        """
        if graph_name not in self._graphs:
            raise ValueError(f"Graph {graph_name} not found")
        
        app = self.compile_graph(graph_name)
        
        # Track iterations
        iteration = 0
        current_state = initial_state
        
        while iteration < max_iterations:
            # Check budget
            token_state = current_state.get("token_opt_state")
            if token_state:
                remaining = token_state.token_budget_remaining
                if remaining <= 0:
                    logger.info("Stopping: token budget exhausted (%d remaining)", remaining)
                    current_state["should_stop"] = True
                    break
            
            # Check stop flag
            if current_state.get("should_stop"):
                break
            
            # Execute one step
            try:
                result = await app.ainvoke(current_state)
                current_state = result
                
                iteration += 1
                
                # Update token state iteration count
                if token_state:
                    token_state.workflow_turn = iteration
                    
            except Exception as exc:
                logger.error("Graph execution failed: %s", exc)
                current_state["should_stop"] = True
                break
        
        return current_state


class G16LangGraphRuntime:
    """G16 LangGraph runtime middleware."""
    
    def __init__(self):
        self._runtime: Optional[LangGraphRuntime] = None
    
    def _get_runtime(self) -> LangGraphRuntime:
        if self._runtime is None:
            self._runtime = LangGraphRuntime()
        return self._runtime
    
    async def spawn_sub_agent(
        self,
        ctx: RequestContext,
        agent_name: str,
        agent_config: Dict,
        budget_allocation: int,
    ) -> Dict:
        """
        Spawn a sub-agent with allocated budget.
        
        Args:
            agent_name: Name of sub-agent to spawn
            agent_config: Configuration for sub-agent
            budget_allocation: Token budget allocated to sub-agent
            
        Returns:
            Sub-agent result with cost tracking
        """
        cfg = ctx.config.get("groups", {}).get("G16_agent_arch", {})
        if not cfg.get("langgraph_enabled", False):
            return {"error": "LangGraph runtime not enabled"}
        
        try:
            runtime = self._get_runtime()
            
            # Create sub-agent state with allocated budget
            sub_state = LangGraphAgentState(
                messages=ctx.messages.copy(),
                token_opt_state=InterAgentState(
                    token_budget_remaining=budget_allocation,
                    workflow_turn=1,
                    max_iterations=agent_config.get("max_iterations", 5),
                ),
                sub_agents=[],
                cost_accumulated_usd=0.0,
                should_stop=False,
                next_node=None,
            )
            
            # Execute sub-agent graph
            result_state = await runtime.execute(
                agent_name,
                sub_state,
                max_iterations=agent_config.get("max_iterations", 5),
            )
            
            # Calculate cost
            tokens_used = budget_allocation - (result_state.get("token_opt_state", {}).token_budget_remaining or 0)
            cost_usd = tokens_used * 0.00001  # Approximate cost per token
            
            return {
                "success": True,
                "result": result_state.get("messages", []),
                "tokens_used": tokens_used,
                "cost_usd": cost_usd,
                "iterations": result_state.get("token_opt_state", {}).workflow_turn or 0,
            }
            
        except Exception as exc:
            logger.error("Sub-agent spawn failed: %s", exc)
            return {
                "success": False,
                "error": str(exc),
            }
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        """
        Process request with LangGraph routing.
        
        Enables conditional routing based on request characteristics.
        """
        cfg = ctx.config.get("groups", {}).get("G16_agent_arch", {})
        if not cfg.get("langgraph_enabled", False):
            return ctx
        
        # Check for routing directive in params
        route = ctx.params.get("x_route_to")
        if route:
            logger.info("[%s] G16 routing to: %s", ctx.request_id, route)
            ctx.params["_routed_by_langgraph"] = True
        
        return ctx


# Cost modeling utilities
class AgentCostModel:
    """Cost modeling for agent decomposition decisions."""
    
    @staticmethod
    def estimate_cost(
        input_tokens: int,
        output_tokens: int,
        model: str,
        iterations: int = 1,
    ) -> Dict:
        """
        Estimate cost for an agent workflow.
        
        Returns:
            {
                "input_cost_usd": float,
                "output_cost_usd": float,
                "total_cost_usd": float,
                "cost_per_iteration": float,
            }
        """
        # Pricing per 1K tokens (approximate)
        pricing = {
            "gpt-4o": {"input": 0.005, "output": 0.015},
            "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
            "gpt-4-5": {"input": 0.075, "output": 0.15},
        }
        
        # Find matching pricing tier
        model_pricing = pricing.get("gpt-4o-mini")
        for tier, price in pricing.items():
            if tier in model.lower():
                model_pricing = price
                break
        
        input_cost = (input_tokens / 1000) * model_pricing["input"] * iterations
        output_cost = (output_tokens / 1000) * model_pricing["output"] * iterations
        
        return {
            "input_cost_usd": input_cost,
            "output_cost_usd": output_cost,
            "total_cost_usd": input_cost + output_cost,
            "cost_per_iteration": (input_cost + output_cost) / iterations,
        }
    
    @staticmethod
    def compare_strategies(
        monolithic_tokens: int,
        decomposed_tokens: List[int],
        model: str,
    ) -> Dict:
        """
        Compare monolithic vs decomposed agent costs.
        
        Returns recommendation for architecture choice.
        """
        mono_cost = AgentCostModel.estimate_cost(
            monolithic_tokens, 
            monolithic_tokens // 2,  # Assume 2:1 output ratio
            model,
            1,
        )
        
        decomp_cost = sum(
            AgentCostModel.estimate_cost(
                tokens,
                tokens // 2,
                model,
                1,
            )["total_cost_usd"]
            for tokens in decomposed_tokens
        )
        
        savings = mono_cost["total_cost_usd"] - decomp_cost
        
        return {
            "monolithic_cost_usd": mono_cost["total_cost_usd"],
            "decomposed_cost_usd": decomp_cost,
            "savings_usd": savings,
            "savings_percent": (savings / mono_cost["total_cost_usd"]) * 100,
            "recommendation": "decompose" if savings > 0 else "monolithic",
        }


# Example usage
if __name__ == "__main__":
    # Test cost model
    cost = AgentCostModel.estimate_cost(1000, 500, "gpt-4o-mini", 3)
    print(f"Estimated cost: ${cost['total_cost_usd']:.4f}")
    
    # Test comparison
    comparison = AgentCostModel.compare_strategies(
        5000,  # Monolithic
        [1500, 1500, 1500],  # Decomposed
        "gpt-4o-mini",
    )
    print(f"Recommendation: {comparison['recommendation']}")
    print(f"Savings: ${comparison['savings_usd']:.4f} ({comparison['savings_percent']:.1f}%)")
