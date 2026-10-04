"""
G08 · Tool Definition Loading
Stage: Into the LLM
Saving: 20–70% system prompt tokens
Technique: Inject only tools relevant to the current step via intent classification.
           Full tool registry lives in GCS; only matching subset loaded per request.
           Eliminates 2,000–5,000 tokens/call from full tool injection.
           
Features:
  - MCP (Model Context Protocol) lazy-load manifest for dynamic tool discovery
  - A daily pruning of registry tools the model has stopped calling, per tenant
    (run_tool_pruning_loop; report only until pruning.dry_run_first is false)
  - Tool usage analytics for optimization
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

from middleware import RequestContext
from middleware import cache_floor
from middleware.g01_compression import _is_faithful_compression
from middleware.prose_compress import compress_descriptions_in_place
from savings.calculator import count_tools_tokens

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
# The daily pass runs once per UTC day across replicas: the first to SET this key runs it.
_TOOL_PRUNING_DAY = "tok_opt:tool:pruning_day:"
# ON by default since 2026-09-06 (E17): a tool absent from the registry is always kept
# (fail-open — see the `reg_entry`/`tool_intents` filter below), so intent pruning is a
# no-op until the operator populates their own tools. Compressing description prose is the
# one part of G08 that helps a customer who has not done that. Matches the shipped default
# in config.yaml.template — the code's own fallback must not silently disagree with it if a
# hand-edited config or a per-tenant overlay omits the key.
_COMPRESS_DESCRIPTIONS_DEFAULT = True


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
    # `gs://${CONFIG_GCS_BUCKET}/...` with the bucket unset - every local / non-GCP deploy,
    # because the template ships that path - arrives here as `gs:///config/...`. There is
    # nothing to fetch, but constructing the GCS client anyway runs Google's Application
    # Default Credentials discovery, which outside GCP waits on the metadata server for ~3 s.
    # That wait was synchronous, on the request path, once per worker per registry-cache TTL:
    # it was the 3.3-3.5 s G08 stage in the local latency dashboards (2026-09-18).
    gcs_bucket = registry_path[5:].partition("/")[0] if registry_path.startswith("gs://") else ""
    try:
        if gcs_bucket:
            from google.cloud import storage
            bucket, blob = registry_path[5:].split("/", 1)
            client = storage.Client()
            data = client.bucket(bucket).blob(blob).download_as_text()
            tools = yaml.safe_load(data).get("tools", [])
        else:
            if registry_path.startswith("gs://"):
                logger.debug("G08 registry_path %s names no GCS bucket - using the local registry",
                             registry_path)
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


def _named_by_request(params: Dict[str, Any], messages: List[Dict]) -> Set[str]:
    """Tools the request itself names: ``tool_choice`` (or the legacy ``function_call``) and
    every tool an earlier assistant turn called. Pruning one is a provider 400: tool_choice
    would name a tool missing from ``tools``, or a call in the history would have no tool."""
    names: Set[str] = set()

    def _add(spec: Any) -> None:
        if isinstance(spec, dict):
            fn = spec.get("function")
            name = fn.get("name") if isinstance(fn, dict) else spec.get("name")
            if isinstance(name, str) and name:
                names.add(name)

    _add(params.get("tool_choice"))
    _add(params.get("function_call"))
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                _add(call)
            _add(msg.get("function_call"))
    return names


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


def _prune_candidate(meta: Any, now: float, days: int) -> bool:
    """A registry tool the model has stopped calling: first offered at least ``days`` days
    ago, offered within the last ``days`` days, and not called within them. A tool with no
    record, or no first offer on record, is never one: it may not have been sent yet, and
    pruning it would hide it from its first use."""
    window = days * 86400
    try:
        first = float(meta["first_seen"])
        offered = float(meta["last_used"])
        called = float(meta.get("last_called") or 0)
    except (KeyError, TypeError, ValueError):
        return False
    return now - first >= window and now - offered <= window and now - called > window


def called_tool_names(response: Any) -> Set[str]:
    """The tools a chat-completion response calls: every choice's ``tool_calls`` and the
    legacy ``function_call``."""
    names: Set[str] = set()
    choices = response.get("choices") if isinstance(response, dict) else None
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        specs = [c.get("function") for c in message.get("tool_calls") or []
                 if isinstance(c, dict)] + [message.get("function_call")]
        names.update(s["name"] for s in specs
                     if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"])
    return names


async def record_called_tools(ctx: Any, names: Iterable[str]) -> None:
    """Record that the model called these tools: ``last_called`` on each one's usage record,
    which the pruning reads. Only the registry tools G08 offered in this request count
    (``ctx.g08_offered_tools``), so a caller's own tool names never become keys. One
    pipelined round trip, none when nothing qualifies; never raises."""
    offered = set(getattr(ctx, "g08_offered_tools", None) or ())
    called = sorted(name for name in set(names or ()) if name in offered)
    if not called:
        return
    try:
        prefix = getattr(ctx, "redis_prefix", "")
        now, ttl = str(time.time()), _tool_usage_ttl_days() * 86400
        pipe = _get_redis().pipeline(transaction=False)
        for name in called:
            meta = f"{prefix}{_TOOL_USAGE_PREFIX}{name}:meta"
            pipe.hset(meta, "last_called", now)
            pipe.expire(meta, ttl)
        await pipe.execute()
    except Exception as exc:
        logger.debug("Recording the called tools failed: %s", exc)


class ScheduledToolPruning:
    """Pruning of the registry tools the model has stopped calling, from usage records."""

    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._pruning_interval_hours = _TOOL_PRUNING_INTERVAL_HOURS
        self._inactivity_threshold_days = _TOOL_INACTIVITY_THRESHOLD_DAYS

    async def record_tool_usage(self, tool_names: List[str], ctx: RequestContext) -> None:
        """Record that these tools were offered: the last offer (``last_used``), the first
        (``first_seen``, kept until a pruning restarts the tool's history) and a count, the
        record should_prune_tool reads with record_called_tools' ``last_called``. One
        pipelined round trip, and each record expires after tool_usage_ttl_days.

        It used to add a sorted-set member per tool per request, kept 90 days and read by
        nothing, in five sequential round trips per tool, and the per-tool record never
        expired. Agent traffic filled Redis, which then evicts other keys (spend caps,
        quotas, rate limits) or refuses writes."""
        if not self.redis or not tool_names:
            return
        try:
            prefix = getattr(ctx, "redis_prefix", "")
            now = str(time.time())
            ttl = _tool_usage_ttl_days() * 86400
            pipe = self.redis.pipeline(transaction=False)
            for tool_name in tool_names:
                meta = f"{prefix}{_TOOL_USAGE_PREFIX}{tool_name}:meta"
                pipe.hset(meta, "last_used", now)
                pipe.hsetnx(meta, "first_seen", now)
                pipe.hincrby(meta, "total_calls", 1)
                pipe.expire(meta, ttl)
            await pipe.execute()
        except Exception as exc:
            logger.debug("Tool usage recording failed: %s", exc)

    async def clear_pruned(self, tool_names: List[str], prefix: str = "") -> None:
        """Lift the pruned mark from tools a request named: it wants them."""
        if not self.redis or not tool_names:
            return
        try:
            await self.redis.delete(*[f"{prefix}{_TOOL_MANIFEST_PREFIX}{name}"
                                      for name in tool_names])
        except Exception as exc:
            logger.debug("Lifting pruned marks failed: %s", exc)

    async def pruned_tools(self, tool_names: List[str], prefix: str = "") -> Set[str]:
        """The tools among ``tool_names`` that the scheduled job marked pruned, looked up in
        one pipelined round trip (it was one HGET per tool, on every request)."""
        if not self.redis or not tool_names:
            return set()
        try:
            pipe = self.redis.pipeline(transaction=False)
            for tool_name in tool_names:
                pipe.hget(f"{prefix}{_TOOL_MANIFEST_PREFIX}{tool_name}", "status")
            statuses = await pipe.execute()
        except Exception as exc:
            logger.debug("Pruned-status lookup failed: %s", exc)
            return set()
        return {name for name, status in zip(tool_names, statuses, strict=False)
                if status == "pruned"}
    
    async def should_prune_tool(self, tool_name: str, prefix: str = "") -> bool:
        """Whether the model has stopped calling this tool (see _prune_candidate). Until
        2026-10-02 a tool with no usage record counted as inactive, so a first run would
        have hidden every registry tool a tenant had not sent yet."""
        if not self.redis:
            return False
        try:
            meta = await self.redis.hgetall(f"{prefix}{_TOOL_USAGE_PREFIX}{tool_name}:meta")
        except Exception as exc:
            logger.debug("Pruning check failed: %s", exc)
            return False
        return _prune_candidate(meta, time.time(), _inactivity_threshold_days())

    async def get_inactive_tools(self, registry_tools: List[str], prefix: str = "") -> List[str]:
        """The registry tools the model has stopped calling."""
        inactive = []
        for tool_name in registry_tools:
            if await self.should_prune_tool(tool_name, prefix=prefix):
                inactive.append(tool_name)
        return inactive

    async def run_scheduled_pruning(self, dry_run: bool = False, prefix: str = "",
                                    registry_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        """Find, and unless ``dry_run`` mark pruned, the registry tools the model has stopped
        calling, for one tenant (``prefix``: t:<id>:, empty for the default tenant), from
        ``registry_tools`` (default: the deployment's registry). A mark lapses after the
        inactivity window and the tool's history starts again, so the tool is offered and
        judged afresh; a request that names the tool lifts the mark at once."""
        if not self.redis:
            return {"status": "no_redis", "pruned": []}
        lock_key = f"{prefix}{_TOOL_PRUNING_LOCK}"
        try:
            lock_acquired = await self.redis.set(lock_key, str(time.time()), ex=3600, nx=True)
            if not lock_acquired:
                return {"status": "already_running", "pruned": []}
            if registry_tools is None:
                registry_tools = [r.get("name") for r in _load_registry({}) if r.get("name")]
            inactive = await self.get_inactive_tools(registry_tools, prefix=prefix)
            if dry_run:
                await self.redis.delete(lock_key)
                return {"status": "dry_run", "would_prune": inactive}
            if inactive:
                window, now = _inactivity_threshold_days() * 86400, str(time.time())
                pipe = self.redis.pipeline(transaction=False)
                for tool_name in inactive:
                    mark = f"{prefix}{_TOOL_MANIFEST_PREFIX}{tool_name}"
                    pipe.hset(mark, "status", "pruned")
                    pipe.hset(mark, "pruned_at", now)
                    pipe.expire(mark, window)
                    pipe.hdel(f"{prefix}{_TOOL_USAGE_PREFIX}{tool_name}:meta", "first_seen")
                await pipe.execute()
            await self.redis.delete(lock_key)
            logger.info("Scheduled pruning completed: %d tools pruned", len(inactive))
            return {"status": "completed", "pruned": inactive, "count": len(inactive)}
        except Exception as exc:
            logger.error("Scheduled pruning failed: %s", exc)
            try:
                await self.redis.delete(lock_key)
            except Exception as release_exc:
                logger.debug("Releasing the pruning lock failed (it expires in an hour): %s",
                             release_exc)
            return {"status": "error", "error": str(exc), "pruned": []}


_DAILY_SCHEDULE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")


def _daily_time(schedule: Any) -> Optional[Tuple[int, int]]:
    """(hour, minute) of a daily cron schedule "M H * * *", or None for any other form."""
    m = _DAILY_SCHEDULE.match(str(schedule or ""))
    if not m or int(m.group(1)) > 59 or int(m.group(2)) > 23:
        return None
    return int(m.group(2)), int(m.group(1))


def _seconds_until_next_run(schedule: Any, now: float) -> float:
    """Seconds from ``now`` to the next pass: each day at pruning.schedule's time (UTC) for
    the daily form "M H * * *"; for any other schedule, TOOL_PRUNING_INTERVAL_HOURS (24)."""
    at = _daily_time(schedule)
    if at is None:
        return float(_TOOL_PRUNING_INTERVAL_HOURS * 3600)
    current = datetime.fromtimestamp(now, timezone.utc)
    nxt = current.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if nxt <= current:
        nxt += timedelta(days=1)
    return (nxt - current).total_seconds()


async def run_pruning_pass(get_config, redis=None) -> Dict[str, Dict[str, Any]]:
    """One daily pass: for every tenant with tool usage records, find the registry tools the
    model has stopped calling, against that tenant's own registry (its operator
    ``tenants.<id>`` overlay applies). Reports them (a log line and
    token_opt_tool_pruning_candidates) and, once ``pruning.dry_run_first`` is false, marks
    them. Once per UTC day across replicas. Returns each tenant's result."""
    config = get_config() or {}
    pruning = (((config.get("groups") or {}).get("G8_tools") or {}).get("pruning") or {})
    if not pruning.get("enabled", False):
        return {}
    redis = redis if redis is not None else _get_redis()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not await redis.set(f"{_TOOL_PRUNING_DAY}{day}", "1", ex=86400, nx=True):
        return {}                                   # this day's pass has run already
    dry_run = bool(pruning.get("dry_run_first", True))
    from middleware import apply_operator_overlay
    from middleware.g18_observability import TOOL_PRUNING_CANDIDATES
    prefixes = set()
    async for key in redis.scan_iter(match=f"*{_TOOL_USAGE_PREFIX}*:meta", count=500):
        prefixes.add(key[:key.find(_TOOL_USAGE_PREFIX)])
    results: Dict[str, Dict[str, Any]] = {}
    for prefix in sorted(prefixes):
        tenant = prefix[2:-1] if prefix.startswith("t:") and prefix.endswith(":") else "default"
        tenant_g8 = ((apply_operator_overlay(config, tenant).get("groups") or {})
                     .get("G8_tools") or {})
        registry = await asyncio.to_thread(_load_registry, tenant_g8)   # may read GCS
        names = [r.get("name") for r in registry if isinstance(r, dict) and r.get("name")]
        result = await ScheduledToolPruning(redis).run_scheduled_pruning(
            dry_run=dry_run, prefix=prefix, registry_tools=names)
        results[tenant] = result
        if result.get("status") not in ("dry_run", "completed"):
            continue
        found = result.get("would_prune", result.get("pruned")) or []
        TOOL_PRUNING_CANDIDATES.labels(tenant_id=tenant).set(len(found))
        if found:
            logger.info("G08 tool pruning (%s), tenant %s: %s",
                        "report only" if dry_run else "marked pruned", tenant, ", ".join(found))
    return results


async def run_tool_pruning_loop(get_config, *, sleep=asyncio.sleep, clock=time.time) -> None:
    """The scheduled tool pruning, started at proxy startup: wait for the next
    pruning.schedule time, run_pruning_pass, repeat. Never raises out of the loop; a
    cancellation ends it."""
    warned = False
    while True:
        schedule = ""
        try:
            schedule = (((get_config() or {}).get("groups") or {}).get("G8_tools") or {}) \
                .get("pruning", {}).get("schedule", "")
        except Exception as exc:
            logger.debug("G08 pruning: the schedule could not be read (%s); using the "
                         "default interval", exc)
        if schedule and _daily_time(schedule) is None and not warned:
            logger.warning("G08 pruning.schedule %r is not the daily form 'M H * * *': the "
                           "pruning runs every %d hours instead", schedule,
                           _TOOL_PRUNING_INTERVAL_HOURS)
            warned = True
        await sleep(_seconds_until_next_run(schedule, clock()))
        try:
            await run_pruning_pass(get_config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("G08 tool pruning pass failed: %s", exc)


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
        names = [tool.get("function", {}).get("name", "") for tool in tools]
        pruned = await self._get_pruning().pruned_tools([n for n in names if n], prefix)
        return [tool for tool, name in zip(tools, names, strict=True) if name not in pruned]
    
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G8_tools", {})
        if not cfg.get("enabled", False):
            return ctx

        existing_tools = ctx.params.get("tools", [])
        if not existing_tools:
            return ctx  # No tools in request — nothing to prune

        # Shared packed-form estimator (2026-08-08): raw str(t) counting ran ~2.4x
        # over provider billing, so G08's recorded savings could exceed the tools'
        # entire baseline contribution. One estimator, one truth (same as G16).
        tokens_before = count_tools_tokens(existing_tools, ctx.model)

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
        names = [
            tool.get("function", {}).get("name", "") if isinstance(tool, dict) else ""
            for tool in existing_tools
        ]
        # Only tools the registry or an MCP manifest names take part in pruning and usage
        # records: the scheduled job prunes registry tools only, and a caller's own tool
        # names must not become Redis keys (they could mint any number of them).
        known = [name for name in names if name and name in all_available_tools]
        pruning = self._get_pruning()
        pruned_names = await pruning.pruned_tools(known, getattr(ctx, "redis_prefix", ""))

        # Filter tools: keep those whose intents overlap with classified intents — and always
        # the ones the request itself names (dropping those is a provider 400).
        required = _named_by_request(ctx.params, ctx.messages)
        relevant: List[Dict] = []
        used: List[str] = []
        for tool, tool_name in zip(existing_tools, names, strict=True):
            if tool_name and tool_name in required:
                relevant.append(tool)
                if tool_name in all_available_tools:
                    used.append(tool_name)
                continue
            if tool_name in pruned_names:
                continue

            # Find registry entry for this tool
            reg_entry = all_available_tools.get(tool_name)
            tool_intents = (
                reg_entry.get("intents", ["default"]) if isinstance(reg_entry, dict) else ["default"]
            )
            if any(i in tool_intents for i in intents) or "default" in tool_intents:
                relevant.append(tool)
                if tool_name and tool_name in all_available_tools:
                    used.append(tool_name)
        # A pruned tool the request names (tool_choice, or an earlier call) comes back.
        named_pruned = sorted(pruned_names & required)
        if named_pruned:
            await pruning.clear_pruned(named_pruned, getattr(ctx, "redis_prefix", ""))
        # The response side records which of these the model calls (the pruning's signal).
        ctx.g08_offered_tools = list(used)
        await pruning.record_tool_usage(used, ctx)

        # Optional: compress tool/function DESCRIPTION prose (deterministic, zero-LLM,
        # regex-only via prose_compress). Tool manifests ride EVERY agentic request and
        # G08 otherwise passes descriptions verbatim, so this trims tokens on every
        # tool-carrying call. Deep-copy first — never mutate the caller's tool dicts. Each
        # description is held to G01's faithfulness guard, as every prompt compression is:
        # one that drops a negation or a bound is sent as written.
        desc_saved_chars = 0
        if cfg.get("compress_descriptions", _COMPRESS_DESCRIPTIONS_DEFAULT) and relevant:
            import copy
            fields = cfg.get("compress_description_fields", ["description"])
            relevant = copy.deepcopy(relevant)
            desc_saved_chars = compress_descriptions_in_place(
                relevant, fields, accept=_is_faithful_compression)

        pruned = len(existing_tools) - len(relevant)

        # Prefix-cache floor (backlog #41). Tool definitions sit inside the span a
        # provider measures against its minimum cacheable size — first in the block on a
        # marker-based provider — so trimming descriptions or dropping tools can push it
        # under the floor and silently forfeit the whole prefix discount, exactly as
        # prompt compression can. No rate dial here, so the choice is binary: take the
        # trim, or keep the tool list whole. Inert unless a floor was reserved.
        _floor_state = cache_floor.get(ctx)
        if (pruned > 0 or desc_saved_chars > 0) and _floor_state.active:
            _span_before = cache_floor.span_tokens(ctx, ctx.messages)
            _held = ctx.params.get("tools")
            ctx.params["tools"] = relevant
            _span_after = cache_floor.span_tokens(ctx, ctx.messages)
            ctx.params["tools"] = _held
            if not cache_floor.allows_shrink(ctx, _span_before, _span_after, "G08"):
                cache_floor.record_action(ctx, cache_floor.ACTION_PRESERVED)
                logger.info(
                    "[%s] G08: keeping the tool list whole — trimming it would take the "
                    "cacheable span from %d to %d tokens, under this provider's %d-token "
                    "minimum cacheable size",
                    ctx.request_id, _span_before, _span_after, _floor_state.floor,
                )
                relevant = existing_tools
                pruned, desc_saved_chars = 0, 0

        if pruned > 0 or desc_saved_chars > 0:
            ctx.params["tools"] = relevant
            tokens_after = count_tools_tokens(relevant, ctx.model)
            notes: List[str] = []
            if pruned > 0:
                notes.append(f"pruned {pruned}/{len(existing_tools)} tools (intents={intents})")
            if desc_saved_chars > 0:
                notes.append(f"compressed descriptions (−{desc_saved_chars} chars)")
            ctx.savings.add_step(
                GROUP,
                "Tool registry: " + "; ".join(notes),
                tokens_before,
                tokens_after,
            )
            logger.debug(
                "[%s] G08 tools: %d → %d (%s)",
                ctx.request_id,
                len(existing_tools),
                len(relevant),
                "; ".join(notes),
            )

        return ctx
