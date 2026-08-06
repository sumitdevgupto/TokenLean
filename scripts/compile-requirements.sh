#!/usr/bin/env bash
# =============================================================================
# compile-requirements.sh — regenerate the pinned dependency lockfiles.
# =============================================================================
# Source of truth:   src/proxy/requirements.in   tests/requirements-test.in
# Compiled output:   src/proxy/requirements.txt  tests/requirements-test.txt
#
# The compile runs inside python:3.11-slim (the SAME base as src/proxy/Dockerfile)
# so the resolve matches what the image actually installs — a pin set generated
# on Windows/macOS would miss Linux-only wheels and platform markers.
#
# Deliberate exclusions from the pinned output (via --unsafe-package + the
# nvidia-* sweep): torch — the Dockerfile preinstalls the CPU build first and a
# PyPI pin would drag the ~2.5 GB CUDA wheel back into the image; triton and
# nvidia-* — CUDA-only companions of that wheel; uvloop — Linux-only, installed
# on Linux via uvicorn[standard]'s own environment marker, and an unconditional
# pin would break `pip install -r` on Windows dev machines.
# tests/unit/test_requirements_pinned.py enforces these exclusions in CI, so a
# Dependabot regeneration that reintroduces them fails the PR loudly.
#
# Usage: bash scripts/compile-requirements.sh          (needs Docker running)
# =============================================================================
set -euo pipefail

# pwd -W: Git Bash on Windows must hand Docker a d:/... path, not /d/...
ROOT_DIR="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
IMAGE="python:3.11-slim"   # keep in lockstep with src/proxy/Dockerfile's FROM

# MSYS_NO_PATHCONV: stop Git Bash rewriting container paths like -w /w into W:/
# (harmless no-op on real Linux/macOS shells).
MSYS_NO_PATHCONV=1 docker run --rm -v "${ROOT_DIR}:/w" -w /w "$IMAGE" bash -c '
  set -euo pipefail
  pip install -q pip-tools

  cd src/proxy
  pip-compile --no-strip-extras \
      --unsafe-package torch --unsafe-package triton --unsafe-package uvloop \
      --output-file requirements.txt requirements.in
  # Belt for the guard test: a future torch bump may drag new CUDA runtime
  # packages into the closure under fresh names — never pin any of them.
  sed -i "/^nvidia-/d" requirements.txt

  cd ../../tests
  pip-compile --no-strip-extras \
      -c ../src/proxy/requirements.txt \
      --output-file requirements-test.txt requirements-test.in
'

echo "OK: pinned src/proxy/requirements.txt + tests/requirements-test.txt"
