#!/usr/bin/env bash
# =============================================================================
# validate-templates.sh — G02 prompt-template token-budget gate
# -----------------------------------------------------------------------------
# Fails the build if any registered prompt template exceeds its token budget.
# Budgets are declared in config/config.yaml.template under
#   groups.G2_template_registry.budgets.<template-id>
# Template content lives in config/templates/<template-id>.yaml.
#
# Wired into:
#   - local build : scripts/local/build-local.sh
#   - GCP  build  : scripts/gcp/gcp-deploy.sh  (validate_templates step)
#   - Cloud Build : ci/cloudbuild.yaml / ci/cloudbuild-images-only.yaml
#
# Soft-skips (exit 0 with a warning) when python or pyyaml is unavailable on the
# host so a missing local toolchain never blocks a build — it only fails on a
# real budget violation.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/ci/ -> repo root is two levels up.
export REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Prefer python3, fall back to python (Windows/Git-Bash often only has `python`).
PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "${PYTHON}" ]; then
  echo "[WARN] python not found on PATH — skipping G02 template budget gate"
  exit 0
fi
if ! "${PYTHON}" -c "import yaml" >/dev/null 2>&1; then
  echo "[WARN] pyyaml not installed for ${PYTHON} — skipping G02 template budget gate"
  echo "       (install with: ${PYTHON} -m pip install pyyaml tiktoken)"
  exit 0
fi

# Requires: pyyaml (mandatory), tiktoken (optional — falls back to ~4 chars/token)
"${PYTHON}" - <<'PYEOF'
import sys, os, yaml

try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False

def count_tokens(text: str, model: str = "gpt-4o") -> int:
    if HAS_TIKTOKEN and any(f in model.lower() for f in ("gpt", "o1", "o3")):
        try:
            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(text))
        except Exception:
            pass
    # Universal fallback: ~4 chars per token
    return max(1, (len(text) + 3) // 4)

repo_root = os.environ["REPO_ROOT"]
config_path = os.path.join(repo_root, "config", "config.yaml.template")

with open(config_path) as f:
    config = yaml.safe_load(f)

budgets = config.get("groups", {}).get("G2_template_registry", {}).get("budgets", {})
if not budgets:
    print("No template budgets configured — skipping G02 validation")
    sys.exit(0)

templates_dir = os.path.join(repo_root, "config", "templates")
failed = 0

for template_id, budget in budgets.items():
    template_file = os.path.join(templates_dir, f"{template_id}.yaml")
    if not os.path.exists(template_file):
        print(f"\033[1;33m[WARN]\033[0m Template '{template_id}' registered but file not found at {template_file}")
        continue

    with open(template_file) as f:
        tmpl = yaml.safe_load(f)

    system_prompt = tmpl.get("system_prompt", "")
    system_tokens = count_tokens(system_prompt)
    max_system = budget.get("system_prompt_max", 0)

    total_tokens = count_tokens(system_prompt + tmpl.get("example_input", ""))
    max_total = budget.get("total_input_max", 0)

    ok = True
    if max_system and system_tokens > max_system:
        print(f"\033[0;31m[FAIL]\033[0m {template_id}: system_prompt {system_tokens}t > budget {max_system}t")
        ok = False; failed += 1
    if max_total and total_tokens > max_total:
        print(f"\033[0;31m[FAIL]\033[0m {template_id}: total_input {total_tokens}t > budget {max_total}t")
        ok = False; failed += 1
    if ok:
        print(f"\033[0;32m[OK]\033[0m   {template_id}: system={system_tokens}t total={total_tokens}t (budgets: sys<={max_system} total<={max_total})")

if failed:
    print(f"\n\033[0;31m{failed} template(s) exceed token budget — build blocked.\033[0m")
    print("Increase the budget in config.yaml.template with documented justification,")
    print("or reduce the template content.")
    sys.exit(1)
else:
    print(f"\n\033[0;32mAll {len(budgets)} template(s) within budget.\033[0m")

PYEOF
