"""
G08 MCP Tool Loading — Lazy-Load Manifest Protocol

Implements MCP (Model Context Protocol) lazy loading:
1. Dynamic manifest loading from MCP servers
2. Lazy tool loading on first use
3. Scheduled pruning of unused tools
4. Tool metadata caching
"""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)
GROUP = "G08"


@dataclass
class MCPTool:
    """MCP tool definition."""
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Optional[Callable] = None
    server_url: str = ""
    last_used: float = 0.0
    use_count: int = 0


class MCPManifestLoader:
    """Load and cache MCP server manifests."""
    
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._manifest_cache: Dict[str, Dict] = {}
        self._tool_cache: Dict[str, MCPTool] = {}
        self._loaded_servers: Set[str] = set()
    
    async def load_manifest(self, server_url: str, force_refresh: bool = False) -> Optional[Dict]:
        """
        Load manifest from MCP server.
        
        Caches manifests to avoid repeated fetches.
        """
        cache_key = f"mcp:manifest:{self._hash_url(server_url)}"
        
        # Check memory cache
        if not force_refresh and server_url in self._manifest_cache:
            return self._manifest_cache[server_url]
        
        # Check Redis cache
        if not force_refresh and self.redis:
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    manifest = json.loads(cached)
                    self._manifest_cache[server_url] = manifest
                    return manifest
            except Exception as exc:
                logger.debug("MCP manifest cache miss: %s", exc)
        
        # Fetch from server
        try:
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                manifest_url = f"{server_url}/.well-known/mcp-manifest.json"
                async with session.get(manifest_url, timeout=10) as resp:
                    if resp.status == 200:
                        manifest = await resp.json()
                        
                        # Cache the manifest
                        self._manifest_cache[server_url] = manifest
                        if self.redis:
                            await self.redis.setex(
                                cache_key,
                                3600,  # 1 hour TTL
                                json.dumps(manifest),
                            )
                        
                        self._loaded_servers.add(server_url)
                        logger.info("Loaded MCP manifest from %s", server_url)
                        return manifest
                    else:
                        logger.warning(
                            "Failed to load MCP manifest from %s: HTTP %d",
                            server_url,
                            resp.status,
                        )
                        
        except Exception as exc:
            logger.warning("MCP manifest fetch failed for %s: %s", server_url, exc)
        
        return None
    
    async def get_tool(
        self,
        tool_name: str,
        server_url: str,
        lazy_load: bool = True,
    ) -> Optional[MCPTool]:
        """
        Get a tool by name.
        
        If lazy_load is True, the tool is loaded on first use.
        """
        cache_key = f"{server_url}:{tool_name}"
        
        # Check if already loaded
        if cache_key in self._tool_cache:
            tool = self._tool_cache[cache_key]
            tool.last_used = time.time()
            tool.use_count += 1
            return tool
        
        if not lazy_load:
            return None
        
        # Lazy load from manifest
        manifest = await self.load_manifest(server_url)
        if not manifest:
            return None
        
        # Find tool in manifest
        tools = manifest.get("tools", [])
        for tool_def in tools:
            if tool_def.get("name") == tool_name:
                tool = MCPTool(
                    name=tool_name,
                    description=tool_def.get("description", ""),
                    input_schema=tool_def.get("inputSchema", {}),
                    server_url=server_url,
                    last_used=time.time(),
                    use_count=1,
                )
                
                self._tool_cache[cache_key] = tool
                logger.info("Lazy-loaded MCP tool: %s from %s", tool_name, server_url)
                return tool
        
        return None
    
    async def list_available_tools(self, server_url: str) -> List[str]:
        """List all available tools from a server."""
        manifest = await self.load_manifest(server_url)
        if not manifest:
            return []
        
        tools = manifest.get("tools", [])
        return [t.get("name") for t in tools if t.get("name")]
    
    def _hash_url(self, url: str) -> str:
        """Generate short hash for URL."""
        return hashlib.sha256(url.encode()).hexdigest()[:16]
    
    async def get_unused_tools(self, days: int = 30) -> List[MCPTool]:
        """Get tools that haven't been used in specified days."""
        cutoff = time.time() - (days * 86400)
        unused = []
        
        for key, tool in self._tool_cache.items():
            if tool.last_used < cutoff:
                unused.append(tool)
        
        return unused
    
    async def prune_unused_tools(self, days: int = 30) -> int:
        """Remove unused tools from cache."""
        unused = await self.get_unused_tools(days)
        
        for tool in unused:
            cache_key = f"{tool.server_url}:{tool.name}"
            if cache_key in self._tool_cache:
                del self._tool_cache[cache_key]
                logger.info("Pruned unused MCP tool: %s", tool.name)
        
        return len(unused)


class G08MCPLoader:
    """G08 MCP lazy loading middleware."""
    
    def __init__(self):
        self._manifest_loader: Optional[MCPManifestLoader] = None
    
    def _get_loader(self, redis) -> MCPManifestLoader:
        if self._manifest_loader is None:
            self._manifest_loader = MCPManifestLoader(redis)
        return self._manifest_loader
    
    async def process_request(self, ctx) -> Any:
        cfg = ctx.config.get("groups", {}).get("G8_tool_loading", {})
        if not cfg.get("mcp_enabled", False):
            return ctx
        
        try:
            from cache.redis_pool import get_redis
            redis = get_redis()
            loader = self._get_loader(redis)
            
            # Check for MCP tool calls in messages
            for msg in ctx.messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg.get("tool_calls") or []:
                        if isinstance(tc, dict):
                            func = tc.get("function", {})
                            tool_name = func.get("name", "")
                            
                            # Check if this is an MCP tool
                            mcp_servers = cfg.get("mcp_servers", [])
                            for server_url in mcp_servers:
                                tool = await loader.get_tool(
                                    tool_name,
                                    server_url,
                                    lazy_load=True,
                                )
                                if tool:
                                    logger.debug(
                                        "[%s] MCP tool resolved: %s",
                                        ctx.request_id,
                                        tool_name,
                                    )
                                    break
            
        except Exception as exc:
            logger.warning("[%s] G08 MCP loading failed: %s", ctx.request_id, exc)
        
        return ctx


# Scheduled pruning job
async def prune_stale_tools(redis_dsn: str = "", days: int = 30):
    """
    Prune MCP tools unused for specified days.
    
    Run as scheduled job (e.g., via Cloud Scheduler).
    """
    from cache.redis_pool import get_redis as _get_redis
    redis = _get_redis()
    loader = MCPManifestLoader(redis)
    pruned = await loader.prune_unused_tools(days)
    logger.info("Pruned %d stale MCP tools (unused > %d days)", pruned, days)


if __name__ == "__main__":
    # Test manifest loading
    async def test():
        loader = MCPManifestLoader()
        
        # This would need a real MCP server
        # manifest = await loader.load_manifest("http://localhost:8080")
        # print(json.dumps(manifest, indent=2))
        
        print("MCP loader initialized")
    
    asyncio.run(test())
