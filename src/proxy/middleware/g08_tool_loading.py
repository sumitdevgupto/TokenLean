"""
G08 · Tool Definition Loading
Stage: Into the LLM
Saving: 20–70% system prompt tokens
Technique: Inject only tools relevant to the current step via intent classification.
           Full tool registry lives in GCS; only matching subset loaded per request.
           Eliminates 2,000–5,000 tokens/call from full tool injection.
           
Features:
  - MCP (Model Context Protocol) lazy-load manifest for dynamic tool discovery
  - Scheduled pruning job for inactive tools
  - Tool usage analytics for optimization
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import yaml

from middleware import RequestContext
from savings.calculator import estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G08"

import os as _os
# WS21: keyed by registry_path - a per-tenant registry_path override must never
# be served another tenant's cached tool list. {path: (tools, loaded_at)}
_registry_cache: Dict[str, tuple] = {}
_REGISTRY_CACHE_TTL = int(_os.getenv("TOOL_REGISTRY_CACHE_TTL_SECONDS", "300"))
_MCP_MANIFEST_CACHE_TTL = int(_os.getenv("MCP_MANIFEST_CACHE_TTL_SECONDS", "300"))
_MCP_HTTP_TIMEOUT = float(_os.getenv("MCP_HTTP_TIMEOUT_SECONDS", "10.0"))
_TOOL_USAGE_TTL_DAYS = int(_os.getenv("TOOL_USAGE_TTL_DAYS", "90"))
_TOOL_PRUNING_INTERVAL_HOURS = int(_os.getenv("TOOL_PRUNING_INTERVAL_HOURS", "24"))
_TOOL_INACTIVITY_THRESHOLD_DAYS = int(_os.getenv("TOOL_INACTIVITY_THRESHOLD_DAYS", "30"))

# Redis key prefixes for tool management
_TOOL_MANIFEST_PREFIX = "tok_opt:tool:manifest:"
_TOOL_USAGE_PREFIX = "tok_opt:tool:usage:"
_TOOL_PRUNING_LOCK = "tok_opt:tool:pruning_lock"


# ── Config-first knob resolution (item 83a) ───────────────────────────────────
# The env-derived module constants above are now the *fallback defaults*; a value
# set under `groups.G8_tools.*` in the hot-reloaded proxy config wins. This keeps
# existing TOOL_*/MCP_* env deployments working while making the documented config
# keys actually take effect. Resolved per-use so config hot-reload applies. These
# are infra knobs (cache TTLs / timeouts / pruning), so resolution is global
# (get_proxy_config) rather than per-tenant.
def _g8_cfg() -> Dict:
    try:
        from config_loader import get_proxy_config
        return get_proxy_config().get("groups", {}).get("G8_tools", {}) or {}
    except Exception:
        return {}

def _registry_cache_ttl() -> int:
    return int(_g8_cfg().get("registry_cache_ttl_seconds", _REGISTRY_CACHE_TTL))

def _mcp_manifest_ttl() -> int:
    return int(_g8_cfg().get("mcp_manifest_cache_ttl_seconds", _MCP_MANIFEST_CACHE_TTL))

def _mcp_http_timeout() -> float:
    return float(_g8_cfg().get("mcp_http_timeout_seconds", _MCP_HTTP_TIMEOUT))

def _tool_usage_ttl_days() -> int:
    return int(_g8_cfg().get("tool_usage_ttl_days", _TOOL_USAGE_TTL_DAYS))

def _inactivity_threshold_days() -> int:
    return int(_g8_cfg().get("pruning", {}).get("inactivity_threshold_days", _TOOL_INACTIVITY_THRESHOLD_DAYS))


def _load_registry(cfg: Dict) -> List[Dict]:
    import time

    now = time.monotonic()
    registry_path = cfg.get("registry_path", "")
    cache_key = registry_path or "(default)"
    hit = _registry_cache.get(cache_key)
    if hit and hit[0] and (now - hit[1]) < _registry_cache_ttl():
        return hit[0]
    tools = []
    try:
        if registry_path.startswith("gs://"):
            from google.cloud import storage
            bucket, blob = registry_path[5:].split("/", 1)
            client = storage.Client()
            data = client.bucket(bucket).blob(blob).download_as_text()
            tools = yaml.safe_load(data).get("tools", [])
        else:
            local = os.getenv("TOOL_REGISTRY_PATH", "config/tool-registry.yaml")
            with open(local) as f:
                tools = yaml.safe_load(f).get("tools", [])
    except Exception as exc:
        logger.warning("G08 could not load tool registry from %s: %s", registry_path, exc)
        # Local fallback so the proxy stays functional without GCS/ADC access
        # (e.g. local dev, CI, or ROI ablation runs).
        local = os.getenv("TOOL_REGISTRY_PATH", "config/tool-registry.yaml")
        try:
            with open(local) as f:
                tools = yaml.safe_load(f).get("tools", [])
            logger.info("G08 loaded local fallback tool registry from %s", local)
        except Exception as fallback_exc:
            logger.warning("G08 local fallback registry also failed: %s", fallback_exc)

    # A registry file with an explicit `tools:` (null) makes `.get("tools", [])`
    # return None; coerce so callers can always iterate the result.
    tools = tools or []
    _registry_cache[cache_key] = (tools, now)
    return tools


def _classify_intent(messages: List[Dict]) -> List[str]:
    """Simple keyword-based intent extraction from latest user message."""
    intents = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = str(msg.get("content", "")).lower()
            keywords_map = {
                "search": ["search", "find", "lookup", "query"],
                "calculate": ["calculate", "compute", "math", "sum", "count"],
                "fetch_data": ["fetch", "retrieve", "get", "load", "read"],
                "write": ["write", "create", "generate", "draft", "compose"],
                "email": ["email", "send", "notify", "message"],
                "calendar": ["schedule", "calendar", "meeting", "appointment"],
                "code": ["code", "function", "class", "implement", "debug"],
            }
            for intent, keys in keywords_map.items():
                if any(k in content for k in keys):
                    intents.append(intent)
            break
    return intents or ["default"]


def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


class MCPLazyLoadManifest:
    """MCP (Model Context Protocol) lazy-load manifest for dynamic tool discovery."""
    
    def __init__(self, server_url: str, tool_filter: Optional[List[str]] = None):
        self.server_url = server_url
        self.tool_filter = tool_filter or []
        self._tools_cache: Optional[List[Dict]] = None
        self._cache_time: float = 0
        self._cache_ttl: int = _MCP_MANIFEST_CACHE_TTL
    
    async def get_tools(self) -> List[Dict]:
        """Fetch tools from MCP server with caching."""
        now = time.time()
        if self._tools_cache and (now - self._cache_time) < _mcp_manifest_ttl():
            return self._tools_cache
        
        try:
            import httpx
            async with httpx.AsyncClient(timeout=_mcp_http_timeout()) as client:
                # MCP manifest endpoint
                resp = await client.get(f"{self.server_url}/.well-known/mcp-manifest")
                resp.raise_for_status()
                manifest = resp.json()
                
                tools = []
                for tool_def in manifest.get("tools", []):
                    tool_name = tool_def.get("name", "")
                    # Apply filter if specified
                    if self.tool_filter and tool_name not in self.tool_filter:
                        continue
                    
                    # Convert MCP format to OpenAI function format
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": tool_def.get("description", ""),
                            "parameters": tool_def.get("parameters", {}),
                        }
                    })
                
                self._tools_cache = tools
                self._cache_time = now
                return tools
        except Exception as exc:
            logger.debug("MCP manifest fetch failed: %s", exc)
            return []
    
    def get_tool_hash(self) -> str:
        """Get hash of current tool set for change detection."""
        if not self._tools_cache:
            return ""
        tools_json = json.dumps(self._tools_cache, sort_keys=True)
        return hashlib.sha256(tools_json.encode()).hexdigest()[:16]


class ScheduledToolPruning:
    """Background pruning of inactive tools based on usage analytics."""
    
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._pruning_interval_hours = _TOOL_PRUNING_INTERVAL_HOURS
        self._inactivity_threshold_days = _TOOL_INACTIVITY_THRESHOLD_DAYS
    
    async def record_tool_usage(self, tool_name: str, ctx: RequestContext) -> None:
        """Record tool invocation for analytics."""
        if not self.redis:
            return
        
        try:
            key = f"{getattr(ctx, 'redis_prefix', '')}{_TOOL_USAGE_PREFIX}{tool_name}"
            now = time.time()
            
            # Add to sorted set with timestamp
            await self.redis.zadd(key, {str(now): now})
            
            # Update last used
            await self.redis.hset(f"{key}:meta", "last_used", str(now))
            await self.redis.hincrby(f"{key}:meta", "total_calls", 1)
            
            # Trim to last N days
            cutoff = now - (_tool_usage_ttl_days() * 86400)
            await self.redis.zremrangebyscore(key, 0, cutoff)

            # Set expiry
            await self.redis.expire(key, _tool_usage_ttl_days() * 86400)
        except Exception as exc:
            logger.debug("Tool usage recording failed: %s", exc)
    
    async def should_prune_tool(self, tool_name: str, prefix: str = "") -> bool:
        """Check if tool should be pruned due to inactivity."""
        if not self.redis:
            return False
        
        try:
            key = f"{prefix}{_TOOL_USAGE_PREFIX}{tool_name}"
            meta = await self.redis.hgetall(f"{key}:meta")
            
            if not meta:
                # No usage recorded - consider inactive
                return True
            
            last_used = float(meta.get("last_used", 0))
            inactivity_days = (time.time() - last_used) / 86400
            
            return inactivity_days > _inactivity_threshold_days()
        except Exception as exc:
            logger.debug("Pruning check failed: %s", exc)
            return False
    
    async def get_inactive_tools(self, registry_tools: List[str], prefix: str = "") -> List[str]:
        """Get list of inactive tools from registry."""
        inactive = []
        for tool_name in registry_tools:
            if await self.should_prune_tool(tool_name, prefix=prefix):
                inactive.append(tool_name)
        return inactive
    
    async def run_scheduled_pruning(self, dry_run: bool = False, prefix: str = "") -> Dict[str, Any]:
        """Run scheduled pruning job (call periodically via cron/scheduler)."""
        if not self.redis:
            return {"status": "no_redis", "pruned": []}
        # WS21: prefix scopes the run to one tenant (t:<id>:); empty = legacy global.
        lock_key = f"{prefix}{_TOOL_PRUNING_LOCK}"
        
        try:
            # Acquire lock to prevent concurrent pruning
            lock_acquired = await self.redis.set(
                lock_key, 
                str(time.time()), 
                ex=3600,  # 1 hour lock
                nx=True
            )
            if not lock_acquired:
                return {"status": "already_running", "pruned": []}
            
            # Get all tools from registry
            registry = _load_registry({})
            all_tools = [r.get("name") for r in registry if r.get("name")]
            
            # Find inactive tools
            inactive = await self.get_inactive_tools(all_tools, prefix=prefix)
            
            if dry_run:
                await self.redis.delete(lock_key)
                return {"status": "dry_run", "would_prune": inactive}
            
            # Mark tools as pruned (soft delete)
            pruned = []
            for tool_name in inactive:
                await self.redis.hset(
                    f"{prefix}{_TOOL_MANIFEST_PREFIX}{tool_name}", 
                    "status", 
                    "pruned"
                )
                await self.redis.hset(
                    f"{prefix}{_TOOL_MANIFEST_PREFIX}{tool_name}", 
                    "pruned_at", 
                    str(time.time())
                )
                pruned.append(tool_name)
            
            await self.redis.delete(lock_key)
            
            logger.info("Scheduled pruning completed: %d tools pruned", len(pruned))
            return {"status": "completed", "pruned": pruned, "count": len(pruned)}
            
        except Exception as exc:
            logger.error("Scheduled pruning failed: %s", exc)
            if self.redis:
                await self.redis.delete(lock_key)
            return {"status": "error", "error": str(exc), "pruned": []}


class G08ToolLoading:
    """Tool loading with MCP manifest support and usage tracking."""
    
    def __init__(self):
        self._mcp_manifests: Dict[str, MCPLazyLoadManifest] = {}
        self._pruning: Optional[ScheduledToolPruning] = None
    
    def _get_pruning(self) -> ScheduledToolPruning:
        if self._pruning is None:
            try:
                redis = _get_redis()
                self._pruning = ScheduledToolPruning(redis)
            except Exception:
                self._pruning = ScheduledToolPruning(None)
        return self._pruning
    
    async def _load_mcp_tools(self, cfg: Dict) -> List[Dict]:
        """Load tools from MCP manifest servers."""
        # `.get(key, [])` returns the default only when the key is ABSENT; a config
        # block with an explicit `mcp_servers:` (null) yields None and breaks the
        # loop below. `or []` collapses both absent and null to an empty list.
        mcp_servers = cfg.get("mcp_servers") or []
        all_tools = []

        for server_config in mcp_servers:
            url = server_config.get("url", "")
            filter_tools = server_config.get("filter_tools") or []
            
            if not url:
                continue
            
            # Get or create manifest handler
            if url not in self._mcp_manifests:
                self._mcp_manifests[url] = MCPLazyLoadManifest(url, filter_tools)
            
            manifest = self._mcp_manifests[url]
            tools = await manifest.get_tools()
            all_tools.extend(tools)
            
            logger.debug("Loaded %d tools from MCP server %s", len(tools), url)
        
        return all_tools
    
    async def _apply_pruning(self, tools: List[Dict], prefix: str = "") -> List[Dict]:
        """Remove pruned tools from the list."""
        pruning = self._get_pruning()
        if not pruning.redis:
            return tools
        
        active_tools = []
        for tool in tools:
            tool_name = tool.get("function", {}).get("name", "")
            
            # Check if tool is pruned
            try:
                status = await pruning.redis.hget(
                    f"{prefix}{_TOOL_MANIFEST_PREFIX}{tool_name}", 
                    "status"
                )
                if status == "pruned":
                    logger.debug("Skipping pruned tool: %s", tool_name)
                    continue
            except Exception:
                pass
            
            active_tools.append(tool)
        
        return active_tools
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G8_tools", {})
        if not cfg.get("enabled", False):
            return ctx

        existing_tools = ctx.params.get("tools", [])
        if not existing_tools:
            return ctx  # No tools in request — nothing to prune

        tokens_before = sum(
            estimate_tokens(str(t), ctx.model) for t in existing_tools
        )

        # Load tools from registry and MCP manifests
        registry = _load_registry(cfg)
        mcp_tools = await self._load_mcp_tools(cfg)
        
        # Merge registry and MCP tools
        all_available_tools = {t.get("name", ""): t for t in registry}
        for tool in mcp_tools:
            tool_name = tool.get("function", {}).get("name", "")
            if tool_name:
                all_available_tools[tool_name] = tool

        # Apply scheduled pruning
        intents = _classify_intent(ctx.messages)

        # Filter tools: keep those whose intents overlap with classified intents
        relevant: List[Dict] = []
        for tool in existing_tools:
            tool_name = (
                tool.get("function", {}).get("name", "")
                if isinstance(tool, dict)
                else ""
            )
            
            # Check if tool is pruned
            is_pruned = False
            try:
                pruning = self._get_pruning()
                if pruning.redis:
                    status = await pruning.redis.hget(
                        f"{getattr(ctx, 'redis_prefix', '')}{_TOOL_MANIFEST_PREFIX}{tool_name}", "status"
                    )
                    if status == "pruned":
                        is_pruned = True
            except Exception:
                pass
            
            if is_pruned:
                continue
            
            # Find registry entry for this tool
            reg_entry = all_available_tools.get(tool_name)
            tool_intents = (
                reg_entry.get("intents", ["default"]) if isinstance(reg_entry, dict) else ["default"]
            )
            if any(i in tool_intents for i in intents) or "default" in tool_intents:
                relevant.append(tool)
                # Record usage for analytics
                await self._get_pruning().record_tool_usage(tool_name, ctx)

        if len(relevant) < len(existing_tools):
            pruned = len(existing_tools) - len(relevant)
            ctx.params["tools"] = relevant
            tokens_after = sum(estimate_tokens(str(t), ctx.model) for t in relevant)
            ctx.savings.add_step(
                GROUP,
                f"Tool registry: pruned {pruned}/{len(existing_tools)} tools (intents={intents})",
                tokens_before,
                tokens_after,
            )
            logger.debug(
                "[%s] G08 pruned tools: %d → %d",
                ctx.request_id,
                len(existing_tools),
                len(relevant),
            )

        return ctx
