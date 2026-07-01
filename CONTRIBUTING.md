# Contributing to TokenLean

Thanks for your interest in contributing! This project is an open-core LLM
optimisation proxy that transparently applies a suite of token-reduction
techniques (G0–G28) to every request. Contributions of all kinds are welcome —
bug reports, docs, new optimisation techniques, provider adapters, and tests.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Ways to contribute

- **Report a bug** — open a [Bug Report](.github/ISSUE_TEMPLATE/bug_report.yml) issue.
- **Request a feature** — open a [Feature Request](.github/ISSUE_TEMPLATE/feature_request.yml).
- **Improve docs** — the `docs/` folder and `README.md` are fair game.
- **Add/extend an optimisation** — new `GXX` middleware, provider adapters, or tuning.
- **Report a security issue** — please use [SECURITY.md](SECURITY.md), **not** a public issue.

## Development setup

The proxy is a Python (3.12) FastAPI app. The full stack (Redis, Postgres,
Qdrant, sidecars) runs via Docker Compose.

```bash
# 1. Clone and copy config templates
git clone https://github.com/sumitdevgupto/TokenLean
cd TokenLean
cp config/config.yaml.template config/config.yaml

# 2. Start the local stack
docker compose up -d

# 3. Send a test request (see README for the one-line integration)
```

For local Python work without containers, install the proxy requirements:

```bash
pip install -r src/proxy/requirements.txt -r tests/requirements-test.txt
```

## Running tests

The test suite uses `pytest` (config in `pytest.ini`, which sets the import paths):

```bash
pytest tests/ -q                                   # full suite
pytest tests/ -k "g01 or g05"                      # filter by optimisation group
pytest --cov=src/proxy --cov-report=term-missing   # with coverage
```

**All PRs must pass the test suite and the clean-boot check in CI** before review.

## Adding a new optimisation group (GXX)

Each optimisation is a self-contained middleware in `src/proxy/middleware/`:

1. Create `src/proxy/middleware/gXX_feature_name.py` with a class exposing
   `process_request` (and optionally `process_response`).
2. Register it in `src/proxy/middleware/pipeline.py` at the correct stage.
3. Externalise all parameters to config (`config/params/gXX_*.yaml.template`) —
   **no hardcoded thresholds**.
4. Track savings via `ctx.savings.add_step(...)`.
5. Add tests in `tests/` (`test_gXX_*.py`) — feature behaviour **and** a savings
   assertion. A feature is not done until tests prove the savings.

See `docs/request-flow-diagram.md` for the pipeline order and `docs/config-reference.md`
for the config schema.

## Coding conventions

- **Absolute imports only** within `src/proxy/` (e.g. `from middleware.g01_compression import ...`).
- Mutate `ctx.messages` in place; the original is preserved in `ctx.original_messages`.
- Use `ctx.redis_prefix` / `ctx.qdrant_collection` for all tenant-scoped storage —
  never bare keys.
- Respect short-circuit flags (`ctx.bypassed`, `ctx.cache_hit`, `ctx.skip_groups`).
- Match the style, naming, and comment density of the surrounding code.

## Pull request process

1. Fork and branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Keep PRs focused; one logical change per PR.
3. Fill in the [PR template](.github/PULL_REQUEST_TEMPLATE.md) — what changed, why, and how it was tested.
4. Ensure `pytest tests/ -q` is green locally and CI passes.
5. Update relevant docs (`docs/`, `README.md`) when behaviour or config changes.

## License of contributions

This project is licensed under **Apache-2.0** (see [LICENSE](LICENSE)). Unless you
state otherwise, any contribution you submit is provided under the same Apache-2.0
terms (per section 5 of the License). Please do not submit code under incompatible
or copyleft (GPL/AGPL) licenses.

Thank you for helping make LLM usage cheaper for everyone! 🚀
