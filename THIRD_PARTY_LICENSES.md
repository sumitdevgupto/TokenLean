# Third-Party Licenses

The TokenLean — Token Optimisation Framework is licensed under **Apache-2.0** (see [LICENSE](LICENSE)).
It depends on, integrates with, and — in its self-hosted Docker deployment — runs
alongside third-party open-source software.

This file covers the **bundled OSS services, sidecars, and models** (run as
separate containers/processes). For the **Python package dependencies** (imported
libraries), see [docs/oss-licenses.md](docs/oss-licenses.md).

## The license rule

Everything TokenLean uses must be free to use and to host: permissively licensed (MIT,
Apache-2.0, BSD, ISC, PSF, PostgreSQL, BSL-1.0, CNRI-Python, Zlib, CC0), or MPL-2.0 for an
unmodified transitive dependency. `scripts/audit_licenses.py` checks every pinned Python
package against it in CI. The one exception is Grafana (AGPL-3.0, below): it runs
unmodified, which the AGPL allows for any use, commercial hosting included.

---

## Bundled OSS services & sidecars (run as separate processes)

These components are pulled as upstream Docker images or run as sidecar services.
They communicate with the proxy over the network/IPC and are **not linked into or
redistributed as part of** the Apache-2.0 Work.

| Component | Role | License | Linkage |
|---|---|---|---|
| Redis 7.2 | L1/L2 cache, rate limiting, batch streams | BSD-3-Clause — see note | Service (network) |
| PostgreSQL + pgvector | Metering / audit / tenant-config store | PostgreSQL License (permissive), both | Service (network) |
| Qdrant (server) | Vector store for RAG / semantic cache | Apache-2.0 | Service (network) |
| Prometheus | Metrics scraping | Apache-2.0 | Service (network) |
| Alertmanager (GCP deploy) | Alert routing | Apache-2.0 | Service (network) |
| Jaeger | Distributed tracing (OTLP) | Apache-2.0 | Service (network) |
| **Grafana** | Dashboards | **AGPL-3.0** | **Service only — see note** |
| Langfuse (server) | LLM observability backend | MIT, except its `ee/` directories (Langfuse's enterprise license) | Service (network) |
| Apache Tika (`tika-sidecar`) | Document text extraction (G03) | Apache-2.0 | Service (HTTP sidecar) |
| LLMLingua / LLMLingua-2 (`llmlingua-sidecar`) | Prompt compression (G01) | MIT | Service (HTTP sidecar) |
| RouteLLM (`routellm-sidecar`) | Model routing (G06) | Apache-2.0 | Service (HTTP sidecar) |

> **Redis is pinned to 7.2** (`redis:7.2-alpine`, in Docker Compose and on the optional GCP
> Redis VM): the last BSD-3-Clause release. From 7.4 Redis is RSALv2/SSPLv1, outside the rule,
> and the floating `redis:7-alpine` tag had moved to 7.4.11. GCP Memorystore (Redis 7.0) is
> Google's managed service.

> **Grafana (AGPL-3.0) — the one exception to the license rule; service-only, no
> copyleft obligation on this Work.**
> Grafana is deployed as an unmodified upstream Docker image and accessed over
> the network. It is **not** statically or dynamically linked into the proxy, and
> no Grafana source is modified or redistributed here. The AGPL's network-use
> copyleft applies to *modified Grafana*, not to independent applications that
> merely query it. It stays an exception only while it runs unmodified: changing
> Grafana's own code would oblige publishing those changes. Dashboards in `dashboard/` are JSON definitions authored for
> this project and are covered by this repo's Apache-2.0 license. Operators who
> self-host Grafana are responsible for their own Grafana compliance.

---

## Models

Downloaded on first use unless noted. Licenses as declared on each model's Hugging Face
card, or by the LICENSE file in its repository; checked 2026-10-04.

| Model | Used by | License | Notes |
|---|---|---|---|
| `BAAI/bge-small-en-v1.5` | G05 L2 semantic cache; G22 deduplication (with `use_embeddings`) | MIT | Baked into the proxy image |
| `sentence-transformers/all-MiniLM-L6-v2` | G03 / G07 dense embeddings | Apache-2.0 | |
| `Qdrant/bm25` | G03 / G07 sparse (BM25) embeddings | Apache-2.0 | |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | G07 reranking | Apache-2.0 | |
| `microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank` | `llmlingua-sidecar` (G01) | Apache-2.0 | |
| `routellm/bert_gpt4_augmented` | `routellm-sidecar`: `bert`, G06's default router (and its choice when `mf`/`sw_ranking` have no OpenAI key) | Apache-2.0 | Base model `xlm-roberta-base` (MIT) |
| `routellm/mf_gpt4_augmented` | `routellm-sidecar`: `mf` router, only when configured (with an OpenAI key) | **None declared** — outside the rule | The repository holds only the weights: no LICENSE file, no license on the model card |
| `routellm/causal_llm_gpt4_augmented` | `routellm-sidecar`: `causal_llm` router, only when configured | Apache-2.0 **and the Meta Llama 3 Community License** — outside the rule | Its repository ships an Apache-2.0 LICENSE, but its config names `meta-llama/Meta-Llama-3-8B` as the base model |
| RouteLLM arena datasets | `routellm-sidecar`: `sw_ranking` router, only when configured (with an OpenAI key) | Apache-2.0 for `lmsys/lmsys-arena-human-preference-55k` and `routellm/gpt4_judge_battles`; none declared for their two embedding datasets — outside the rule | |
| spaCy `en_core_web_lg` | presidio-analyzer's default model (G29, only with `use_presidio: true`) | MIT | |

---

## Python dependencies (imported libraries)

Full per-package inventory with SPDX identifiers: **[docs/oss-licenses.md](docs/oss-licenses.md)**.

Summary: all directly-imported dependencies are permissive — **MIT**, **BSD-2-Clause**,
**BSD-3-Clause**, or **Apache-2.0**; torch's wheel adds BSL-1.0 and Apache-2.0 WITH
LLVM-exception for the third-party code it bundles. Headline libraries: LiteLLM (MIT),
FastAPI (MIT), Uvicorn (BSD-3), httpx (BSD-3), tiktoken (MIT), qdrant-client (Apache-2.0),
langfuse client (MIT), OpenTelemetry (Apache-2.0), Instructor (MIT), asyncpg (Apache-2.0),
sentence-transformers / fastembed (Apache-2.0), prometheus-client (Apache-2.0 AND
BSD-2-Clause), pydantic (MIT), llmlingua (MIT), routellm (Apache-2.0), unstructured
(Apache-2.0), pdfminer.six (MIT), python-docx (MIT).

### Transitive weak-copyleft (MPL-2.0) — compatible, no action required

Three common transitive dependencies carry MPL-2.0, a **file-level** weak copyleft
that does **not** impose copyleft on the larger Apache-2.0 Work:

| Package | License | Notes |
|---|---|---|
| certifi | MPL-2.0 | Mozilla CA-certificate bundle (data); pulled in by httpx/requests |
| tqdm | MPL-2.0 AND MIT | Progress bars; pulled in by sentence-transformers/fastembed |
| orjson | MPL-2.0 AND (Apache-2.0 OR MIT) | Fast JSON; pulled in through langsmith — by langgraph in the proxy (until langgraph is dropped) and by langchain-text-splitters in the doc pipeline |

---

## Compliance summary

- **No GPL, LGPL, AGPL, or SSPL code is imported into or redistributed as part of this Work.**
  Checked 2026-10-04 against the published license metadata of every release pinned in
  the six lockfiles (the proxy, the tests, both sidecars and both pipelines). The five
  that publish none were checked against their repositories instead: fsspec
  (BSD-3-Clause), google-crc32c (Apache-2.0), py-rust-stemmers (MIT), routellm
  (Apache-2.0), and zep-python (below). `scripts/audit_licenses.py` repeats this check in
  CI on every change, against the license rule above, with those repository-checked
  licenses recorded as overrides. The direct dependencies' inventory is in
  [docs/oss-licenses.md](docs/oss-licenses.md).
- Grafana (AGPL-3.0) is the only component outside the permissive rule; it runs as an
  **independent network service** from the unmodified upstream image. Redis is pinned to
  7.2 (BSD-3-Clause).
- Weak-copyleft transitive deps (certifi, tqdm, orjson — MPL-2.0) are file-level and
  compatible with Apache-2.0 redistribution.
- `zep-python` 2.0.2 ships without a license file or license metadata. The proxy no
  longer imports it, but the image installs it until the next recompile drops it, and the
  license audit reports it as a pending removal; see [docs/oss-licenses.md](docs/oss-licenses.md).
- G06's RouteLLM router defaults to `bert` (Apache-2.0). The `mf` (no license),
  `causal_llm` (Meta Llama 3 derivative) and `sw_ranking` (partly unlicensed data) routers
  fall outside the rule and run only if an operator configures them. See [Models](#models).
- No Python dependency requires attribution in compiled binaries or restricts sublicensing.

*Last verified: 2026-10-04.*
