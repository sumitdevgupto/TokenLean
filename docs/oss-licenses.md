# OSS Dependency Licenses

All **Python package dependencies** used by the Token Optimisation proxy. Every entry includes the SPDX license identifier and the PyPI package name.

> For **bundled OSS services & sidecars** (Redis, Postgres, Qdrant, Grafana, Langfuse, Tika, LLMLingua, RouteLLM, etc. — run as separate containers), the models they load, and the project's overall license posture, see [`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md) at the repo root. Project license: Apache-2.0 ([`LICENSE`](../LICENSE)).

## Core Dependencies (`src/proxy/requirements.in` — the human-edited source; `requirements.txt` is its compiled pin set)

| Package | Version | SPDX License | Notes |
|---|---|---|---|
| litellm | >=1.95.1,<2.0.0 | MIT | LLM provider abstraction |
| boto3 | >=1.34.0 | Apache-2.0 | AWS Bedrock provider lane (litellm `bedrock/`) |
| fastapi | >=0.111.0 | MIT | HTTP framework |
| uvicorn[standard] | >=0.30.0 | BSD-3-Clause | ASGI server |
| httpx | >=0.27.0 | BSD-3-Clause | Async HTTP client |
| pyyaml | >=6.0.3 | MIT | Config parsing |
| redis | >=5.0.0 | MIT | Cache + session store (the async client, `redis.asyncio`, is part of the package) |
| asyncpg | >=0.31.0 | Apache-2.0 | PostgreSQL async driver |
| google-cloud-storage | >=2.16.0 | Apache-2.0 | GCS config/key storage |
| google-cloud-secret-manager | >=2.20.0 | Apache-2.0 | GCP secret access |
| google-cloud-run | >=0.16.1 | Apache-2.0 | GCP Cloud Run admin |
| langfuse | >=2.7.0,<3.0.0 | MIT | LLM observability |
| opentelemetry-sdk | >=1.25.0 | Apache-2.0 | Telemetry SDK |
| opentelemetry-exporter-otlp | >=1.25.0 | Apache-2.0 | OTLP exporter |
| tiktoken | >=0.7.0 | MIT | Token counting (OpenAI) |
| qdrant-client | >=1.12,<1.13 | Apache-2.0 | Vector store client |
| fastembed | >=0.3.0 | Apache-2.0 | Embedding inference |
| sentence-transformers | >=5.6.1 | Apache-2.0 | Dense embeddings + cross-encoder reranking |
| torch | ==2.14.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | What sentence-transformers runs on: the Dockerfile installs this version's CPU build (`requirements.txt` leaves torch out) |
| prometheus-client | >=0.20.0 | Apache-2.0 AND BSD-2-Clause | Metrics exposition |
| instructor | >=1.3.0 | MIT | Structured LLM output |
| jsonschema | >=4.20.0 | MIT | G11 output JSON-schema validation |

## Optional Dependencies

Imported via `try/except`, so the proxy starts without them, and used only when the corresponding feature is enabled. `requirements.txt` pins them with the core set, so the Docker image includes them.

| Package | Version | SPDX License | Used By | Feature |
|---|---|---|---|---|
| mem0ai | >=2.0.7 | Apache-2.0 | G10 | Long-term conversation memory |
| presidio-analyzer | >=2.2 | MIT | G29 | Higher-recall PII backend (`use_presidio: true`); the default regex tier needs no dependency |

## License Verification Sources

Checked 2026-10-03 against the version `requirements.txt` pins for each package (torch: the version `requirements.in` names).

| Package | Verified Via |
|---|---|
| mem0ai | PyPI `license_expression` field: `Apache-2.0` |
| fastembed | GitHub repo (`qdrant/fastembed`) `license.spdx_id` at tag `v0.8.0`: `Apache-2.0`. PyPI's metadata is inconclusive: `license` reads "Apache License" (not an SPDX identifier) and the classifier reads "Other/Proprietary License" |
| torch | `License-Expression` in the METADATA of the 2.14.0 CPU wheel the Dockerfile installs (`download.pytorch.org/whl/cpu`), identical to PyPI's `license_expression` for 2.14.0. PyTorch's own code is BSD-3-Clause; the other terms cover third-party code bundled in the wheel |
| All others | PyPI `license_expression` or `license` field, or OSI classifier |

## Ported / Adapted Code (not a package dependency)

| Source | SPDX | Used By | Notes |
|---|---|---|---|
| [caveman-shrink](https://github.com/JuliusBrussee/caveman) (`src/mcp-servers/caveman-shrink/compress.js`) | MIT | `src/proxy/middleware/prose_compress.py` (→ G01, G08, `scripts/compress_prompts.py`) | Regex prose-compression algorithm (filler/pleasantry/hedge/article stripping with byte-for-byte code/URL/path/identifier protection) ported JS→Python. G11 verbosity presets + `scripts/compress_prompts.py` adapt caveman's terse-output / `caveman-compress` rulesets. |

> MIT permits use, modification and redistribution with attribution; the copyright and permission notice are retained here and in each ported/adapted file's module docstring. The port is original Python (no upstream code copied verbatim beyond the regex patterns), redistributed under this project's Apache-2.0 with the MIT attribution preserved.

## Compliance Notes

- **The license rule:** every dependency must be free to use and to host — permissively licensed (MIT, Apache-2.0, BSD, ISC, PSF, PostgreSQL, BSL-1.0, CNRI-Python, Zlib, CC0), or MPL-2.0 for an unmodified transitive dependency. `scripts/audit_licenses.py` checks every pin of every image and CI lockfile against it in CI, and `tests/unit/test_oss_licenses_doc.py` keeps the tables above in step with `requirements.in`. Grafana is the one exception (below).
- All **imported** Python dependencies are permissive (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0; torch's wheel adds BSL-1.0 and Apache-2.0 WITH LLVM-exception for its bundled third-party code). No GPL, LGPL, AGPL, or SSPL code is imported. (Two transitive deps — `certifi` and `tqdm` — carry MPL-2.0, a file-level weak copyleft that does not affect Apache-2.0 redistribution; see `THIRD_PARTY_LICENSES.md`.)
- Grafana (AGPL-3.0), the one exception to the rule, is deployed as an unmodified upstream container accessed over the network — not linked into this Work. It must stay unmodified: changing Grafana's own code would oblige publishing those changes. See `THIRD_PARTY_LICENSES.md`.
- Google Cloud packages are optional at runtime when `STORAGE_BACKEND=local` (T40). They remain in `requirements.txt` for GCP deployments.
- Both optional dependencies are permissive: `mem0ai` (Apache-2.0) and `presidio-analyzer` (MIT).
- No dependency requires attribution in the binary or restricts sublicensing.
