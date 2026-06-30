"""
G14 Tool Call Combining — Batch parallel tool lookups

Combines multiple independent tool calls into a single round-trip,
reducing token overhead from multiple LLM calls.

Usage:
    - Tools declare dependencies in their metadata
    - Independent tools (no deps) are grouped and executed in parallel
    - Results are combined before returning to LLM
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """Single tool call request."""
    tool_name: str
    tool_input: Dict[str, Any]
    call_id: str
    dependencies: Set[str]  # Other tool calls this depends on


@dataclass
class ToolResult:
    """Result from a tool call."""
    call_id: str
    tool_name: str
    result: Any
    error: Optional[str] = None
    latency_ms: float = 0.0


class ToolCallBatcher:
    """Batch and parallelize independent tool calls."""
    
    def __init__(self, max_parallel: int = 10):
        self.max_parallel = max_parallel
        self._handlers: Dict[str, callable] = {}
    
    def register_handler(self, tool_name: str, handler: callable):
        """Register a handler for a tool."""
        self._handlers[tool_name] = handler
    
    def parse_tool_calls(self, response: Dict[str, Any]) -> List[ToolCall]:
        """Extract tool calls from LLM response."""
        calls = []
        
        tool_calls_data = response.get("tool_calls") or []
        for tc in tool_calls_data:
            if isinstance(tc, dict):
                call_id = tc.get("id", "")
                function_data = tc.get("function", {})
                tool_name = function_data.get("name", "")
                tool_input_str = function_data.get("arguments", "{}")
                
                try:
                    tool_input = json.loads(tool_input_str) if isinstance(tool_input_str, str) else tool_input_str
                except json.JSONDecodeError:
                    tool_input = {}
                
                # Extract dependencies from tool metadata if available
                dependencies = self._get_tool_dependencies(tool_name, tool_input)
                
                calls.append(ToolCall(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    call_id=call_id,
                    dependencies=dependencies,
                ))
        
        return calls
    
    def _get_tool_dependencies(self, tool_name: str, tool_input: Dict) -> Set[str]:
        """Determine dependencies for a tool call."""
        # Check for explicit dependency declaration
        deps = set()
        
        # Some tools depend on others (e.g., get_details depends on search)
        if "depends_on" in tool_input:
            dep_ref = tool_input.pop("depends_on")  # Remove from input
            if isinstance(dep_ref, str):
                deps.add(dep_ref)
            elif isinstance(dep_ref, list):
                deps.update(dep_ref)
        
        return deps
    
    def group_parallelizable(self, calls: List[ToolCall]) -> List[List[ToolCall]]:
        """
        Group tool calls into waves where each wave has no internal dependencies.
        Returns list of waves, where each wave can execute in parallel.
        """
        if not calls:
            return []
        
        # Build dependency graph
        call_map = {c.call_id: c for c in calls}
        remaining = set(c.call_id for c in calls)
        waves = []
        
        while remaining:
            # Find calls with no remaining dependencies
            ready = []
            for call_id in list(remaining):
                call = call_map[call_id]
                # Check if all deps are satisfied
                deps_satisfied = all(
                    d not in remaining and d in call_map
                    for d in call.dependencies
                )
                if deps_satisfied:
                    ready.append(call)
            
            if not ready:
                # Circular dependency or missing dependency
                logger.error("Tool call dependency resolution failed for: %s", remaining)
                break
            
            waves.append(ready)
            for call in ready:
                remaining.remove(call.call_id)
        
        return waves
    
    async def execute_batch(self, calls: List[ToolCall]) -> List[ToolResult]:
        """Execute batch of tool calls with parallelization."""
        # Group into dependency waves
        waves = self.group_parallelizable(calls)
        all_results = []
        
        for wave_num, wave in enumerate(waves):
            logger.debug("Executing wave %d with %d tool calls", wave_num + 1, len(wave))
            
            # Execute wave in parallel
            semaphore = asyncio.Semaphore(self.max_parallel)
            
            async def execute_with_limit(call: ToolCall) -> ToolResult:
                async with semaphore:
                    return await self._execute_single(call)
            
            wave_results = await asyncio.gather(
                *[execute_with_limit(call) for call in wave],
                return_exceptions=True,
            )
            
            # Handle results and errors
            for call, result in zip(wave, wave_results):
                if isinstance(result, Exception):
                    all_results.append(ToolResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        result=None,
                        error=str(result),
                    ))
                else:
                    all_results.append(result)
        
        return all_results
    
    async def _execute_single(self, call: ToolCall) -> ToolResult:
        """Execute a single tool call."""
        import time
        
        start = time.time()
        handler = self._handlers.get(call.tool_name)
        
        if not handler:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                result=None,
                error=f"No handler registered for tool: {call.tool_name}",
                latency_ms=(time.time() - start) * 1000,
            )
        
        try:
            # Execute handler
            if asyncio.iscoroutinefunction(handler):
                result = await handler(call.tool_input)
            else:
                result = handler(call.tool_input)
            
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                result=result,
                latency_ms=(time.time() - start) * 1000,
            )
            
        except Exception as exc:
            logger.error("Tool execution failed for %s: %s", call.tool_name, exc)
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                result=None,
                error=str(exc),
                latency_ms=(time.time() - start) * 1000,
            )
    
    def combine_results(self, results: List[ToolResult]) -> Dict[str, Any]:
        """Combine multiple tool results into single response format."""
        tool_results = []
        errors = []
        
        for r in results:
            if r.error:
                errors.append({
                    "call_id": r.call_id,
                    "tool": r.tool_name,
                    "error": r.error,
                })
            else:
                tool_results.append({
                    "call_id": r.call_id,
                    "tool": r.tool_name,
                    "result": r.result,
                    "latency_ms": r.latency_ms,
                })
        
        return {
            "tool_results": tool_results,
            "errors": errors if errors else None,
            "combined": True,
            "parallel_executed": len(results) > 1,
        }


class G14ToolCombining:
    """G14 tool call combining middleware."""
    
    def __init__(self):
        self.batcher = ToolCallBatcher()
    
    def register_tool_handler(self, tool_name: str, handler: callable):
        """Register a tool handler."""
        self.batcher.register_handler(tool_name, handler)
    
    async def process_response(self, ctx: Any, response: Dict[str, Any]) -> Dict[str, Any]:
        """Process response with tool call combining."""
        cfg = ctx.config.get("groups", {}).get("G14_tool_output", {})
        
        if not cfg.get("combine_tool_calls", False):
            return response
        
        # Check if response has tool calls
        if "tool_calls" not in response:
            return response
        
        # Parse and batch tool calls
        calls = self.batcher.parse_tool_calls(response)
        if len(calls) <= 1:
            # Nothing to combine
            return response
        
        logger.info("[%s] G14 combining %d tool calls", ctx.request_id, len(calls))
        
        # Execute batched
        results = await self.batcher.execute_batch(calls)
        
        # Combine results
        combined = self.batcher.combine_results(results)
        
        # Update response
        response["_tool_combining"] = combined
        
        # Track savings
        ctx.savings.add_step(
            "G14",
            f"Combined {len(calls)} tool calls",
            tokens_before=0,  # Tool calls don't add tokens to savings calc directly
            tokens_after=0,
        )
        
        return response
