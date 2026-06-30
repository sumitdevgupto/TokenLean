<!-- Thanks for contributing! Please fill in the sections below. -->

## What does this PR do?

<!-- A clear, concise description of the change. -->

## Why?

<!-- The motivation / linked issue. Use "Closes #123" to auto-close an issue. -->

## Type of change

- [ ] Bug fix
- [ ] New optimisation group / feature
- [ ] Provider adapter
- [ ] Docs
- [ ] Tests / CI
- [ ] Refactor (no behaviour change)

## How was it tested?

<!-- Commands run and results. e.g. `pytest tests/ -k gXX -q` -->

## Checklist

- [ ] `pytest tests/ -q` passes locally
- [ ] New/changed parameters are externalised to config (no hardcoded thresholds)
- [ ] Savings tracked via `ctx.savings.add_step(...)` (for optimisation changes)
- [ ] Tenant-scoped storage uses `ctx.redis_prefix` / `ctx.qdrant_collection`
- [ ] Docs updated (`docs/`, `README.md`) where behaviour or config changed
- [ ] No secrets, no commercial-layer code, no GPL/AGPL dependencies introduced
