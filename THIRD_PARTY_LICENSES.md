# Third-Party Licenses

The Token Optimisation Framework is licensed under **Apache-2.0** (see [LICENSE](LICENSE)).
It depends on, integrates with, and — in its self-hosted Docker deployment — runs
alongside third-party open-source software.

This file covers the **bundled OSS services, sidecars, and models** (run as
separate containers/processes). For the **Python package dependencies** (imported
libraries), see [docs/oss-licenses.md](docs/oss-licenses.md).

---

## Bundled OSS services & sidecars (run as separate processes)

These components are pulled as upstream Docker images or run as sidecar services.
They communicate with the proxy over the network/IPC and are **not linked into or
redistributed as part of** the Apache-2.0 Work.

| Component | Role | License | Linkage |
|---|---|---|---|
| Redis | L1/L2 cache, rate limiting, batch streams | BSD-3-Clause (≤7.2) | Service (network) |
| PostgreSQL | Metering / audit / tenant-config store | PostgreSQL License (permissive) | Service (network) |
| Qdrant (server) | Vector store for RAG / semantic cache | Apache-2.0 | Service (network) |
| Prometheus | Metrics scraping | Apache-2.0 | Service (network) |
| Jaeger | Distributed tracing (OTLP) | Apache-2.0 | Service (network) |
| **Grafana** | Dashboards | **AGPL-3.0** | **Service only — see note** |
| Langfuse (server) | LLM observability backend | MIT | Service (network) |
| Apache Tika (`tika-sidecar`) | Document text extraction (G03) | Apache-2.0 | Service (HTTP sidecar) |
| LLMLingua / LLMLingua-2 (`llmlingua-sidecar`) | Prompt compression (G01) | MIT | Service (HTTP sidecar) |
| RouteLLM (`routellm-sidecar`) | Model routing (G06) | Apache-2.0 | Service (HTTP sidecar) |
| OpenMeter | Usage metering events (commercial integration) | Apache-2.0 | Service (network) |
| Temporal (server, optional) | Agent/workflow runtime (G16) | MIT | Service (network) |

> **Grafana (AGPL-3.0) — service-only, no copyleft obligation on this Work.**
> Grafana is deployed as an unmodified upstream Docker image and accessed over
> the network. It is **not** statically or dynamically linked into the proxy, and
> no Grafana source is modified or redistributed here. The AGPL's network-use
> copyleft applies to *modified Grafana*, not to independent applications that
> merely query it. Dashboards in `dashboard/` are JSON definitions authored for
> this project and are covered by this repo's Apache-2.0 license. Operators who
> self-host Grafana are responsible for their own Grafana compliance.

---

## Python dependencies (imported libraries)

Full per-package inventory with SPDX identifiers: **[docs/oss-licenses.md](docs/oss-licenses.md)**.

Summary: all directly-imported dependencies are permissive — **MIT**, **BSD-3-Clause**,
or **Apache-2.0**. Headline libraries: LiteLLM (MIT), FastAPI (MIT), Uvicorn (BSD-3),
httpx (BSD-3), tiktoken (MIT), qdrant-client (Apache-2.0), langfuse client (MIT),
OpenTelemetry (Apache-2.0), Temporal SDK (MIT), Instructor (MIT), asyncpg (Apache-2.0),
sentence-transformers / fastembed (Apache-2.0), pydantic (MIT), llmlingua (MIT),
routellm (Apache-2.0), unstructured (Apache-2.0), pdfminer.six (MIT), python-docx (MIT).

### Transitive weak-copyleft (MPL-2.0) — compatible, no action required

Two common transitive dependencies carry MPL-2.0, a **file-level** weak copyleft
that does **not** impose copyleft on the larger Apache-2.0 Work:

| Package | License | Notes |
|---|---|---|
| certifi | MPL-2.0 | Mozilla CA-certificate bundle (data); pulled in by httpx/requests |
| tqdm | MPL-2.0 AND MIT | Progress bars; pulled in by sentence-transformers/fastembed |

---

## Compliance summary

- **No GPL, AGPL, or SSPL code is imported into or redistributed as part of this Work.**
  (Audit method: `importlib.metadata` license/classifier scan across the installed
  environment — see plan item 7.)
- Copyleft components (Grafana AGPL-3.0) are run as **independent network services**
  from unmodified upstream images.
- Weak-copyleft transitive deps (certifi, tqdm — MPL-2.0) are file-level and
  compatible with Apache-2.0 redistribution.
- No dependency requires attribution in compiled binaries or restricts sublicensing.

*Last verified: 2026-06-25.*
