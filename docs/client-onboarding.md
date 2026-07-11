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

### Anthropic SDK / Claude Code (native `/v1/messages`)
The proxy speaks Anthropic natively — point the Claude SDK at it and keep using
`client.messages.create(...)`. Your **proxy** key goes in `x-api-key` (the tenant's
real provider key is resolved server-side); every optimisation still applies.
```python
# Before
from anthropic import Anthropic
client = Anthropic(api_key="sk-ant-...")

# After — only these two values change
import os
from anthropic import Anthropic
client = Anthropic(
    api_key=os.environ["PROXY_API_KEY"],
    base_url=os.environ["PROXY_ENDPOINT"],   # proxy exposes /v1/messages
)
```

### Gemini (native `generateContent`)
Point the Google GenAI SDK's base URL at the proxy; the proxy exposes
`/v1beta/models/{model}:generateContent` (+ `:streamGenerateContent`) and re-serialises
to Gemini's `candidates` shape. Send the **proxy** key via `x-goog-api-key` or `?key=`.
```python
import os
from google import genai
client = genai.Client(
    api_key=os.environ["PROXY_API_KEY"],
    http_options={"base_url": os.environ["PROXY_ENDPOINT"]},
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

## Uploading documents for retrieval (G03)

If your team uses RAG (the `rag_query` hint above), your reference documents need to be ingested
into the vector store first. Ingestion is handled by the **G03 doc pipeline** — a background Cloud
Run Job, not part of the request path.

**How ingestion is triggered today:** the proxy does **not** accept a file directly. A document is
ingested when it lands in the configured **GCS bucket** and the resulting object notification reaches
the proxy's `POST /ingest-doc` webhook (wired via GCS → Pub/Sub push). The Job then downloads,
extracts (Apache Tika / Unstructured), strips boilerplate, chunks, embeds (dense + sparse), and
upserts into Qdrant for retrieval.

Typical flow:

```bash
# 1. Drop your document into the ingestion bucket (ask your platform team for the bucket name)
gsutil cp handbook.pdf gs://<your-ingest-bucket>/docs/handbook.pdf

# 2. The GCS object notification fires POST /ingest-doc automatically → the Job runs.
#    (No further action needed. Supported inputs: PDF, DOCX, HTML, TXT, and other Tika/Unstructured types.)

# 3. Once ingested, retrieval works via the rag_query hint:
```
```python
resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "What is our PTO policy?"}],
    extra_body={"rag_query": "PTO policy"},   # G07 pulls the matching chunks
)
```

> **What's not available:** there is currently **no direct HTTP upload endpoint** on the proxy
> (you cannot `POST` file bytes and have them ingested). Documents must be placed in GCS first.
> Contact your platform team for the ingestion bucket name and write access. Full flow, Job steps,
> and env vars are documented in [request-flow-diagram.md](request-flow-diagram.md#document-ingestion-pipeline-g03).

## Dashboard

View your savings at: `https://grafana-<hash>-uc.a.run.app`

Dashboards: **Per-Call** · **Hourly** · **Daily** · **Weekly** · **Quarterly**

## Support

Slack: `#platform-llm` | Email: platform-team@example.com
