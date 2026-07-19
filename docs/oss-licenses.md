# OSS Dependency Licenses

All **Python package dependencies** used by the Token Optimisation proxy. Every entry includes the SPDX license identifier and the PyPI package name.

> For **bundled OSS services & sidecars** (Redis, Postgres, Qdrant, Grafana, Langfuse, Tika, LLMLingua, RouteLLM, OpenMeter, etc. — run as separate containers) and the project's overall license posture, see [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) at the repo root. Project license: Apache-2.0 ([`LICENSE`](../LICENSE)).

## Core Dependencies (`src/proxy/requirements.txt`)

| Package | Version | SPDX License | Notes |
|---|---|---|---|
| litellm | >=1.40.0 | MIT | LLM provider abstraction |
| fastapi | >=0.111.0 | MIT | HTTP framework |
| uvicorn | >=0.30.0 | BSD-3-Clause | ASGI server |
| httpx | >=0.27.0 | BSD-3-Clause | Async HTTP client |
| pyyaml | >=6.0.0 | MIT | Config parsing |
| redis | >=5.0.0 | MIT | Cache + session store |
| asyncpg | >=0.29.0 | Apache-2.0 | PostgreSQL async driver |
| pgvector | >=0.2.5 | MIT | pgvector Python client |
| google-cloud-storage | >=2.16.0 | Apache-2.0 | GCS config/key storage |
| google-cloud-secret-manager | >=2.20.0 | Apache-2.0 | GCP secret access |
| google-cloud-tasks | >=2.16.0 | Apache-2.0 | GCP task queues |
| google-cloud-run | >=0.10.0 | Apache-2.0 | GCP Cloud Run admin |
| langfuse | >=2.7.0,<3.0.0 | MIT | LLM observability |
| opentelemetry-sdk | >=1.25.0 | Apache-2.0 | Telemetry SDK |
| opentelemetry-exporter-otlp | >=1.25.0 | Apache-2.0 | OTLP exporter |
| tiktoken | >=0.7.0 | MIT | Token counting (OpenAI) |
| qdrant-client | >=1.9.0 | Apache-2.0 | Vector store client |
| fastembed | >=0.3.0 | Apache-2.0 | Embedding inference |
| prometheus-client | >=0.20.0 | Apache-2.0 | Metrics exposition |
| instructor | >=1.3.0 | MIT | Structured LLM output |
| temporalio | >=1.5.0 | MIT | Temporal workflow runtime |

## Optional Dependencies

Imported via `try/except` in middleware. Install only when the corresponding feature group is enabled.

| Package | Version | SPDX License | Used By | Feature |
|---|---|---|---|---|
| headroom-ai | >=0.26.0 | Apache-2.0 | G19, G25 (planned) | Prompt compression, effort routing |
| mem0ai | >=2.0.7 | Apache-2.0 | G10 | Long-term conversation memory |
| zep-python | >=2.0.2 | Apache-2.0 | G10 | Zep memory backend |

## License Verification Sources

| Package | Verified Via |
|---|---|
| headroom-ai | PyPI `license_expression` field: `Apache-2.0` |
| mem0ai | PyPI `license_expression` field: `Apache-2.0` |
| zep-python | GitHub repo (`getzep/zep-python`) `license.spdx_id`: `Apache-2.0` |
| All others | PyPI `license_expression` or `license` field, or OSI classifier |

## Ported / Adapted Code (not a package dependency)

| Source | SPDX | Used By | Notes |
|---|---|---|---|
| [caveman-shrink](https://github.com/JuliusBrussee/caveman) (`src/mcp-servers/caveman-shrink/compress.js`) | MIT | `src/proxy/middleware/prose_compress.py` (→ G01, G08, `scripts/compress_prompts.py`) | Regex prose-compression algorithm (filler/pleasantry/hedge/article stripping with byte-for-byte code/URL/path/identifier protection) ported JS→Python. G11 verbosity presets + `scripts/compress_prompts.py` adapt caveman's terse-output / `caveman-compress` rulesets. |

> MIT permits use, modification and redistribution with attribution; the copyright and permission notice are retained here and in each ported/adapted file's module docstring. The port is original Python (no upstream code copied verbatim beyond the regex patterns), redistributed under this project's Apache-2.0 with the MIT attribution preserved.

## Compliance Notes

- All **imported** Python dependencies are permissive (MIT, BSD-3-Clause, Apache-2.0). No GPL, LGPL, AGPL, or SSPL code is imported. (Two transitive deps — `certifi`, `tqdm` — carry MPL-2.0, a file-level weak copyleft that does not affect Apache-2.0 redistribution; see `THIRD_PARTY_LICENSES.md`.)
- Copyleft *services* run separately: Grafana (AGPL-3.0) is deployed as an unmodified upstream container accessed over the network — not linked into this Work. See `THIRD_PARTY_LICENSES.md`.
- Google Cloud packages are optional at runtime when `STORAGE_BACKEND=local` (T40). They remain in `requirements.txt` for GCP deployments.
- `headroom-ai`, `mem0ai`, and `zep-python` are safe for commercial use under Apache-2.0 and MIT respectively.
- No dependency requires attribution in the binary or restricts sublicensing.
