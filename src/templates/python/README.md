# Python Developer Templates

## Setup

```bash
export PROXY_ENDPOINT=https://token-proxy-<hash>-uc.a.run.app   # from platform team
export PROXY_API_KEY=<your-proxy-key>                             # from platform team
pip install openai langchain-openai langgraph
```

> **Never use LLM provider keys (OpenAI, Anthropic, etc.) directly.**
> The proxy handles all provider authentication. You only need your proxy key.

## Files

| File | Description |
|---|---|
| `agent_basic.py` | Single-turn and session-aware calls via the proxy |
| `agent_langgraph.py` | Multi-agent LangGraph example with typed state (G09) + budget propagation (G17) |

## Reading savings data

Every response includes a `_token_opt` field:

```python
savings = response.model_extra.get("_token_opt", {})
print(f"Saved: {savings['total_abs_saving']} tokens ({savings['total_pct_saving']}%)")
print(f"Cost saving: ${savings['cost_saving_usd']:.6f}")
print(f"Per-step: {savings['step_savings']}")
```
