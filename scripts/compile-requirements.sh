#!/usr/bin/env bash
# =============================================================================
# compile-requirements.sh — regenerate the pinned dependency lockfiles.
# =============================================================================
# Source of truth:   src/proxy/requirements.in   tests/requirements-test.in
#                    src/{llmlingua-sidecar,routellm-sidecar,doc-pipeline,finetune-pipeline}/requirements.in
# Compiled output:   the requirements.txt beside each of them (what each image installs)
#
# The compile runs inside python:3.11-slim (the SAME base as src/proxy/Dockerfile)
# so the resolve matches what the image actually installs — a pin set generated
# on Windows/macOS would miss Linux-only wheels and platform markers.
#
# Deliberate exclusions from the pinned output (via --unsafe-package + the CUDA
# sweep, scripts/drop-cuda-pins.awk): torch — the Dockerfile preinstalls the CPU
# build first and a PyPI pin would drag the ~2.5 GB CUDA wheel back into the image
# (requirements.in names the version it installs, so the pins are resolved against it);
# triton, nvidia-* and cuda-* — CUDA-only companions of that wheel (newer torch
# reaches the runtime through cuda-toolkit and cuda-bindings, which an nvidia-*-only
# sweep let through: the proxy image carried 1.7 GB of it); uvloop — Linux-only,
# installed on Linux via uvicorn[standard]'s own environment marker, and an
# unconditional pin would break `pip install -r` on Windows dev machines.
# tests/unit/test_requirements_pinned.py enforces these exclusions in CI, so a
# Dependabot regeneration that reintroduces them fails the PR loudly. That is
# not theoretical: Dependabot's pip-compile regenerator does a plain re-resolve
# that honors NEITHER --unsafe-package NOR the CUDA sweep (its first live PR,
# #33, re-pinned torch + the full CUDA runtime and went red on the guard), so
# proxy-lockfile version updates are disabled in .github/dependabot.yml and THIS
# SCRIPT is the one sanctioned way to refresh the pins.
#
# Usage: bash scripts/compile-requirements.sh          (needs Docker running)
# =============================================================================
set -euo pipefail

# pwd -W: Git Bash on Windows must hand Docker a d:/... path, not /d/...
ROOT_DIR="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
# The proxy image's own base, digest and all, read from its Dockerfile: the resolve runs on
# exactly the Python the image ships, and a digest bump there moves this with it.
IMAGE="$(sed -n 's/^FROM \([^ ]*\).*/\1/p' "$(dirname "$0")/../src/proxy/Dockerfile" | head -n 1)"
[[ "$IMAGE" == python:*@sha256:* ]] || { echo "src/proxy/Dockerfile's FROM is not a pinned python image: ${IMAGE:-none}" >&2; exit 2; }

# MSYS_NO_PATHCONV: stop Git Bash rewriting container paths like -w /w into W:/
# (harmless no-op on real Linux/macOS shells).
MSYS_NO_PATHCONV=1 docker run --rm -v "${ROOT_DIR}:/w" -w /w "$IMAGE" bash -c '
  set -euo pipefail
  pip install -q pip-tools
  # The CUDA companions of torch are never pinned (nvidia-*, cuda-*), nor their comment blocks.
  sweep() { awk -f /w/scripts/drop-cuda-pins.awk "$1" > "$1.tmp" && mv "$1.tmp" "$1"; }

  cd src/proxy
  pip-compile --no-strip-extras \
      --unsafe-package torch --unsafe-package triton --unsafe-package uvloop \
      --output-file requirements.txt requirements.in
  # Belt for the guard test: a future torch bump may drag new CUDA runtime
  # packages into the closure under fresh names — never pin any of them.
  sweep requirements.txt

  cd ../../tests
  pip-compile --no-strip-extras \
      -c ../src/proxy/requirements.txt \
      --output-file requirements-test.txt requirements-test.in

  # The sidecar and pipeline images, with the same exclusions: two of them install torch
  # CPU-only in their Dockerfile, and a pin would drag in the CUDA build.
  for dir in llmlingua-sidecar routellm-sidecar doc-pipeline finetune-pipeline; do
    cd "/w/src/$dir"
    pip-compile --no-strip-extras \
        --unsafe-package torch --unsafe-package triton --unsafe-package uvloop \
        --output-file requirements.txt requirements.in
    sweep requirements.txt
  done
'

echo "OK: pinned src/proxy, tests and the four sidecar/pipeline requirements.txt"
