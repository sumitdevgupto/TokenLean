# Developer Onboarding — Token Optimisation Proxy

## What is this?

A transparent proxy (run locally or GCP-hosted) that intercepts your LLM calls and automatically
applies 28 token optimisation techniques (G0–G28, G26 reserved — 27 implemented), reducing cost and
latency without changing your code.

## Getting your proxy key

Contact your platform team to receive:
- `PROXY_ENDPOINT` — the proxy URL (e.g. `https://token-proxy-abc123-uc.a.run.app`)
- `PROXY_API_KEY` — your personal/team proxy key

> **You will never receive LLM provider keys.** The proxy handles all provider authentication securely via GCP Secret Manager.

## Integration (one-line change)

### Python
```python
# Before
from openai import OpenAI
client = OpenAI(api_key="sk-openai-...")

# After — only these two values change
from openai import OpenAI
import os
client = OpenAI(
    api_key=os.environ["PROXY_API_KEY"],
    base_url=os.environ["PROXY_ENDPOINT"] + "/v1",
)
```

### Java
```java
// Before
OpenAIClient client = OpenAIOkHttpClient.builder().apiKey("sk-openai-...").build();

// After
OpenAIClient client = OpenAIOkHttpClient.builder()
    .apiKey(System.getenv("PROXY_API_KEY"))
    .baseUrl(System.getenv("PROXY_ENDPOINT") + "/v1")
    .build();
```

### Go
```go
// Before
config := openai.DefaultConfig("sk-openai-...")

// After
config := openai.DefaultConfig(os.Getenv("PROXY_API_KEY"))
config.BaseURL = os.Getenv("PROXY_ENDPOINT") + "/v1"
```

## Reading savings data

Every response includes a `_token_opt` object:

```json
{
  "choices": [...],
  "_token_opt": {
    "baseline_tokens": 450,
    "final_tokens_sent": 220,
    "total_abs_saving": 230,
    "total_pct_saving": 51.1,
    "cache_hit": false,
    "routed_model": "gpt-4o-mini",
    "cost_baseline_usd": 0.002250,
    "cost_actual_usd": 0.000033,
    "cost_saving_usd": 0.002217,
    "step_savings": {
      "G01": { "abs_saving": 85, "pct_saving_vs_baseline": 18.9 },
      "G05": { "abs_saving": 0,  "pct_saving_vs_baseline": 0.0 },
      "G06": { "abs_saving": 0,  "pct_saving_vs_baseline": 0.0, "description": "Routed gpt-4o → gpt-4o-mini" },
      "G10": { "abs_saving": 145, "pct_saving_vs_baseline": 32.2 }
    }
  }
}
```

> **Fair-disclosure note on `cost_*_usd`.** Cost figures are a **config-priced estimate** — token
> counts multiplied by the static `pricing:` table in `config.yaml`. They do **not** model provider
> discounts, prompt-cache/batch credits, or reasoning surcharges, and `baseline_tokens` is a
> counterfactual ("what you would have sent unoptimised"). Treat them as **directional, not
> invoice-grade**. Token counts (`baseline_tokens` / `final_tokens_sent` / `*_abs_saving`) are exact.

## Optional optimisation hints

Pass these in `extra_body` (Python) or `putAdditionalBodyProperty` (Java) for extra savings:

| Parameter | Purpose | G-group |
|---|---|---|
| `x_session_id` | Enable multi-turn memory management | G10 |
| `workflow_id` | Enable token budget propagation across agent turns | G17 |
| `batch_topic` | Queue request for batch processing | G13 |
| `rag_query` | Trigger hybrid RAG retrieval | G07 |
| `template_id` | Reference a registered prompt template | G02 |
| `user` | Your user ID for per-user savings tracking | G18 |

## Dashboard

View your savings at: `https://grafana-<hash>-uc.a.run.app`

Dashboards: **Per-Call** · **Hourly** · **Daily** · **Weekly** · **Quarterly**

## Support

Slack: `#platform-llm` | Email: platform-team@example.com
