# Adding a Provider — Extensibility Guide

The framework ships **10 first-class providers** (OpenAI, Anthropic, Google Gemini, Azure OpenAI,
AWS Bedrock, Mistral, Cohere, xAI/Grok, DeepSeek, Groq). This guide is for adding **any provider not in
that list**. Most need **no code** — just config.

## Decision tree

1. **Does the provider expose an OpenAI-compatible `/v1/chat/completions`** (OpenAI request/response
   shape)? → **Category 1 — config only, no code.**
2. **Else, does LiteLLM support it natively** (it normalises a non-OpenAI shape for you)? →
   **Category 2 — generic adapter, optionally a thin subclass.**
3. **Else, or it needs something the adapter/pipeline can't express** → **Raise a product request.**

How provider selection works: a request's `model` is matched against each `providers[].model_prefixes`
in `config.yaml`; the first match wins, and the matched entry's adapter handles routing/params. No match
→ `proxy.default_provider`.

---

## Category 1 — OpenAI-compatible (config only, zero code)

Examples: Kimi/Moonshot, GLM/Zhipu, Qwen/DashScope, MiniMax, Baichuan, Perplexity, OpenRouter, Together,
Fireworks, a local vLLM/Ollama OpenAI shim.

**Two modes**, both via the built-in `GenericLiteLLMAdapter`:

**Mode A — LiteLLM has a native id for the provider** (cleaner; check the LiteLLM provider list first):
```yaml
# config.yaml
providers:
  - name: perplexity
    adapter: generic
    litellm_prefix: perplexity      # routes the model as "perplexity/<model>"
    model_prefixes: ["sonar"]
pricing:
  sonar: { input: 0.001, output: 0.001 }   # USD per 1k tokens (reporting only)
```

**Mode B — OpenAI-compatible endpoint LiteLLM doesn't list** (Kimi, GLM, most others):
```yaml
providers:
  - name: kimi                       # Moonshot AI
    adapter: generic
    openai_compatible: true
    api_base: https://api.moonshot.ai/v1
    model_prefixes: ["kimi", "moonshot"]
  - name: glm                        # Zhipu AI
    adapter: generic
    openai_compatible: true
    api_base: https://open.bigmodel.cn/api/paas/v4
    model_prefixes: ["glm"]
pricing:
  kimi-k2: { input: 0.0006, output: 0.0025 }
  glm-4.6: { input: 0.0006, output: 0.0022 }
```

**Steps:**
1. Get the provider's base URL and API key.
2. Add a `providers:` entry (Mode A or B above) with `model_prefixes`.
3. Add `pricing:` rows for its headline models (otherwise the savings/ROI metric falls back to the
   `default` price — billing is per-request and unaffected).
4. Set the key: `LLM_KEY_<NAME>` locally (e.g. `LLM_KEY_KIMI`) / `llm-key-<name>` secret on GCP.
5. *(Optional)* knobs — reasoning: add `effort_map[<tier>].<name>` under `G12_reasoning` **and**
   `supports_reasoning: true` on the provider entry; cache discount:
   `G21_cache_alignment.providers.<name>.cache_read_multiplier`; native batch: `native_batch: true`;
   resilience: a per-provider `resilience:` sub-block (e.g. `failure_threshold`, `num_retries`) and
   `resilience.fallbacks.<model>` failover chains (see [config-reference.md](config-reference.md#resilience--1-provider-failover--top-level-section)).
6. Verify: send a test request, confirm routing in the logs and that savings/cost resolve.

`providers:` changes hot-reload; a **new key** needs the secret set + container recreate.

---

## Category 2 — OpenAI-incompatible (in-repo code, no framework change)

- **2a — LiteLLM-native but a different API shape.** Use the generic adapter with `litellm_prefix`;
  LiteLLM does the translation. Add a thin subclass only if a request param needs special mapping.
- **2b — Needs custom param mapping or capabilities.** Write a thin adapter — it is auto-discovered at
  startup (any `providers/*_adapter.py` whose class is decorated with `@register_adapter`):

```python
# src/proxy/providers/acme_adapter.py
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter

@register_adapter("acme")
class AcmeAdapter(GenericLiteLLMAdapter):
    PROVIDER_NAME = "acme"
    LITELLM_PREFIX = "acme"          # routes "acme/<model>"

    # Override only what differs from the OpenAI-shaped defaults, e.g.:
    def map_structured_output(self, format_type, schema=None):
        ...
    def unsupported_params(self):
        return {"logprobs"}
    def extract_usage(self, response):
        ...
```

Capability hooks you can override on any adapter: `build_call` (routing/auth),
`map_structured_output`, `map_reasoning_effort`, `supports_reasoning`, `unsupported_params`,
`extract_usage`, `cache_read_cost_multiplier`, `supports_native_batch`, `requires_api_key`,
`align_prefix`, `requires_json_keyword`. Add a `providers:` entry, `pricing:` rows, and a unit test.

---

## When to raise a product request

Config + a thin subclass cover almost everything. Open a product request when you hit one of these —
they need a framework change:

- **Auth beyond `api_key` / AWS SigV4 / Azure** — mTLS, OAuth token refresh, per-request signing.
- **Provider is neither OpenAI-compatible nor supported by LiteLLM** — needs a brand-new transport/client.
- **A modality the request pipeline doesn't model** — Responses API, realtime/WebSocket streaming,
  embeddings-only, image/audio generation endpoints.
- **A new optimisation/capability hook on `ProviderAdapter`** — a new cache or batch protocol not
  expressible with the existing methods.
- **Pricing/tokenizer semantics the savings model can't represent** — tiered pricing, a cache-write
  surcharge, non-token billing.
- **Anything needing a change to pipeline order or a new middleware interaction.**

**What to include in the request:** the provider's API docs, a sample request/response, the auth scheme,
and the pricing sheet.

---

## Notes & caveats

- **Streaming** (`stream: true`) is **pass-through**: request-side optimisations apply, then the
  provider's chunks are relayed unchanged; the response-side pipeline (G14/G18/G23) is skipped, so
  streamed savings are request-side only and usage is best-effort from the final chunk.
- **Tokenization** for non-OpenAI models uses a char/4 ingress estimate by default (exact served counts
  still come from the provider `usage`); set `savings.non_gpt_tiktoken_fallback: true` for closer
  baselines. This affects the savings-% estimate, not billing.
- **Param hygiene** is automatic: `litellm.drop_params` plus each adapter's `unsupported_params()` strip
  params a provider rejects (e.g. `parallel_tool_calls`/`logprobs` on non-OpenAI).
