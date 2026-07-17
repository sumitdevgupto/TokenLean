# Release Notes — TokenLean

Newest entries first. Each entry is dated (`YYYY-MM-DD`). Bug fixes and feature
enhancements are logged here as they ship. Enterprise-only items are labelled
**[Enterprise]** and link to <https://tokenlean.cbeyond.cloud/>.

<!--
Entry format (add new entries directly BELOW this comment, newest at top):

## YYYY-MM-DD — <one-line summary>
**Type:** Bug fix | Enhancement (OSS) | Enhancement (OSS + Enterprise) | Enhancement [Enterprise]
<details of what changed and why. For Enterprise items, state it explicitly and link
https://tokenlean.cbeyond.cloud/ >
-->

## 2026-07-15 — G31 Context-Trust: indirect (RAG) prompt-injection defence
**Type:** Enhancement (OSS + Enterprise)

New **G31** middleware closes the indirect prompt-injection gap. G30 scans the untrusted user prompt, but retrieval (G07) and memory (G10) append retrieved documents / stored memories into the prompt **after** G30 runs — so a poisoned document in the vector store could previously reach the model un-inspected. G31 re-scans the *assembled* context (`system` / `tool` roles) with the existing `guardrails/injection.py` engine, runs non-bypassably right after the G07/G10/G22 stages, and supports `allow` / `flag` (default, non-mutating) / `block` (content-filter 200) / `strip` (drop only the poisoned injected content) modes. New metric `token_opt_context_trust_events_total{category,action}`. Config: `groups.G31_context_trust` (see `docs/config-reference.md`).

- **OSS:** the scanner engine + static default ruleset ship in every tier; default `flag` mode is non-mutating (savings/token accounting unchanged).
- **[Enterprise]:** the continuously-updated managed red-team ruleset feed (via `extra_rules`) and the Security dashboards/console — <https://tokenlean.cbeyond.cloud/>.
