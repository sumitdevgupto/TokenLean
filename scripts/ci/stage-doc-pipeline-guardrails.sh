#!/usr/bin/env bash
# Stage the proxy's guardrails engine into the doc-pipeline build context.
#
# The doc-pipeline image masks PII at ingest (opt-in INGEST_PII_MODE) with the proxy's own
# engine, and its Dockerfile copies it from src/doc-pipeline/guardrails/, a gitignored
# build-time copy. Every build path runs this first: scripts/gcp/gcp-deploy.sh,
# ci/cloudbuild.yaml and ci/cloudbuild-images-only.yaml. The file list is explicit, so
# nothing else in src/proxy/guardrails/ can reach the image.
#
# Usage: stage-doc-pipeline-guardrails.sh [--clean] [REPO_ROOT]
#   REPO_ROOT defaults to the repository this script is in. --clean removes the staged copy.
set -euo pipefail

FILES=(__init__.py injection.py pii.py)

clean=false
if [[ "${1:-}" == "--clean" ]]; then
  clean=true
  shift
fi
root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
dest="${root}/src/doc-pipeline/guardrails"

# Always start from nothing, so a copy staged by an earlier run can never go stale.
rm -rf "${dest}"
if [[ "${clean}" == "true" ]]; then
  exit 0
fi
mkdir -p "${dest}"
for f in "${FILES[@]}"; do
  cp "${root}/src/proxy/guardrails/${f}" "${dest}/"
done
echo "Staged ${FILES[*]} into ${dest}"
