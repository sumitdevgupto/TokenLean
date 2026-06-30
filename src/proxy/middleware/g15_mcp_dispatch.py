"""
G15 Server-Side Compute — MCP SDK Server Dispatch

Implements MCP (Model Context Protocol) SDK server integration:
1. Dynamic handler dispatch based on response schema type
2. MCP server tool execution
3. Schema-driven response processing
4. Integration with G08 for tool loading
"""
import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G15"


class MCPHandlerRegistry:
    """Registry for MCP tool handlers."""
    
    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._schema_handlers: Dict[str, Callable] = {}
    
    def register_tool(self, tool_name: str, handler: Callable):
        """Register a handler for a specific tool."""
        self._handlers[tool_name] = handler
        logger.info("Registered MCP handler for tool: %s", tool_name)
    
    def register_schema_handler(self, schema_type: str, handler: Callable):
        """
        Register a handler for a schema type.
        
        Schema types are identified by JSON Schema URI or custom type name.
        """
        self._schema_handlers[schema_type] = handler
        logger.info("Registered MCP handler for schema: %s", schema_type)
    
    def get_handler(self, tool_name: str) -> Optional[Callable]:
        """Get handler for tool."""
        return self._handlers.get(tool_name)
    
    def get_handler_by_schema(self, schema: Dict) -> Optional[Callable]:
        """
        Get handler based on response schema.
        
        Matches by schema $id, type, or custom schema identifiers.
        """
        # Check for schema $id
        schema_id = schema.get("$id")
        if schema_id and schema_id in self._schema_handlers:
            return self._schema_handlers[schema_id]
        
        # Check for type-based handler
        schema_type = schema.get("type")
        if schema_type and schema_type in self._schema_handlers:
            return self._schema_handlers[schema_type]
        
        # Check for custom schema identifier
        custom_type = schema.get("x-mcp-handler-type")
        if custom_type and custom_type in self._schema_handlers:
            return self._schema_handlers[custom_type]
        
        return None


class MCPServerDispatch:
    """Dispatch tool calls to MCP servers."""
    
    def __init__(self, registry: MCPHandlerRegistry):
        self.registry = registry
    
    async def dispatch(
        self,
        tool_name: str,
        tool_input: Dict,
        server_url: str,
        timeout: float = 30.0,
    ) -> Dict:
        """
        Dispatch tool call to MCP server.
        
        Returns:
            {
                "success": bool,
                "result": Any,
                "error": Optional[str],
                "execution_time_ms": float,
            }
        """
        start_time = asyncio.get_event_loop().time()
        
        try:
            # Check for local handler first
            local_handler = self.registry.get_handler(tool_name)
            if local_handler:
                if asyncio.iscoroutinefunction(local_handler):
                    result = await local_handler(tool_input)
                else:
                    result = local_handler(tool_input)
                
                execution_time = (asyncio.get_event_loop().time() - start_time) * 1000
                
                return {
                    "success": True,
                    "result": result,
                    "error": None,
                    "execution_time_ms": execution_time,
                    "source": "local",
                }
            
            # Dispatch to remote MCP server
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                tool_endpoint = f"{server_url}/tools/{tool_name}"
                
                async with session.post(
                    tool_endpoint,
                    json=tool_input,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        execution_time = (asyncio.get_event_loop().time() - start_time) * 1000
                        
                        return {
                            "success": True,
                            "result": result,
                            "error": None,
                            "execution_time_ms": execution_time,
                            "source": "remote",
                        }
                    else:
                        error_text = await resp.text()
                        return {
                            "success": False,
                            "result": None,
                            "error": f"HTTP {resp.status}: {error_text}",
                            "execution_time_ms": (asyncio.get_event_loop().time() - start_time) * 1000,
                            "source": "remote",
                        }
                        
        except asyncio.TimeoutError:
            return {
                "success": False,
                "result": None,
                "error": f"Timeout after {timeout}s",
                "execution_time_ms": timeout * 1000,
                "source": "timeout",
            }
        except Exception as exc:
            return {
                "success": False,
                "result": None,
                "error": str(exc),
                "execution_time_ms": (asyncio.get_event_loop().time() - start_time) * 1000,
                "source": "error",
            }
    
    async def dispatch_with_schema_handling(
        self,
        tool_name: str,
        tool_input: Dict,
        response_schema: Dict,
        server_url: str,
    ) -> Dict:
        """
        Dispatch with schema-driven response processing.
        
        Uses schema to select appropriate handler and validate response.
        """
        # Get schema-based handler
        schema_handler = self.registry.get_handler_by_schema(response_schema)
        
        # Dispatch to tool
        dispatch_result = await self.dispatch(tool_name, tool_input, server_url)
        
        if not dispatch_result["success"]:
            return dispatch_result
        
        # Apply schema handler if available
        if schema_handler:
            try:
                processed = await schema_handler(dispatch_result["result"], response_schema)
                dispatch_result["result"] = processed
                dispatch_result["schema_processed"] = True
            except Exception as exc:
                logger.warning("Schema handler failed: %s", exc)
                dispatch_result["schema_processed"] = False
        
        return dispatch_result


class G15MCPDispatch:
    """G15 MCP server dispatch middleware."""
    
    def __init__(self):
        self._registry: Optional[MCPHandlerRegistry] = None
        self._dispatch: Optional[MCPServerDispatch] = None
    
    def _get_registry(self) -> MCPHandlerRegistry:
        if self._registry is None:
            self._registry = MCPHandlerRegistry()
        return self._registry
    
    def _get_dispatch(self) -> MCPServerDispatch:
        if self._dispatch is None:
            self._dispatch = MCPServerDispatch(self._get_registry())
        return self._dispatch
    
    def register_handler(self, tool_name: str, handler: Callable):
        """Register a local handler for a tool."""
        self._get_registry().register_tool(tool_name, handler)
    
    def register_schema_handler(self, schema_type: str, handler: Callable):
        """Register a handler for a schema type."""
        self._get_registry().register_schema_handler(schema_type, handler)
    
    async def process_response(self, ctx: RequestContext, response: Dict) -> Dict:
        """
        Process response with MCP dispatch.
        
        Handles tool calls in the response by dispatching to MCP servers.
        """
        cfg = ctx.config.get("groups", {}).get("G15_server_compute", {})
        if not cfg.get("mcp_dispatch_enabled", False):
            return response
        
        # Check for tool calls
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            return response
        
        dispatch = self._get_dispatch()
        mcp_servers = cfg.get("mcp_servers", [])
        
        results = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                
                # Parse arguments
                args_str = func.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}
                
                # Try each MCP server
                for server_url in mcp_servers:
                    try:
                        result = await dispatch.dispatch(tool_name, args, server_url)
                        if result["success"]:
                            results.append({
                                "tool_call_id": tc.get("id"),
                                "tool_name": tool_name,
                                "result": result["result"],
                                "source": result.get("source"),
                            })
                            break
                    except Exception as exc:
                        logger.debug("MCP dispatch failed for %s: %s", tool_name, exc)
        
        # Add results to response
        if results:
            response["tool_results"] = results
        
        return response


# Example handlers
async def example_database_handler(input_data: Dict) -> Dict:
    """Example handler for database queries."""
    return {"status": "success", "data": []}

async def example_api_handler(input_data: Dict) -> Dict:
    """Example handler for API calls."""
    return {"status": "success", "response": {}}


# Register example handlers
def register_default_handlers(g15: G15MCPDispatch):
    """Register default MCP handlers."""
    g15.register_handler("db_query", example_database_handler)
    g15.register_handler("api_call", example_api_handler)
    
    # Schema-based handlers
    g15.register_schema_handler("object", lambda r, s: r)
    g15.register_schema_handler("array", lambda r, s: r)


if __name__ == "__main__":
    # Test dispatch
    async def test():
        g15 = G15MCPDispatch()
        register_default_handlers(g15)
        
        registry = g15._get_registry()
        handler = registry.get_handler("db_query")
        print(f"Handler registered: {handler is not None}")
    
    asyncio.run(test())
