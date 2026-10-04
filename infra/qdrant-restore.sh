#!/bin/bash
# Command of Qdrant's Cloud Run container (main.tf: google_cloud_run_v2_service.qdrant). The
# collections live in the container's own filesystem, so a new revision or a restart starts
# Qdrant empty. This restores the newest snapshot of each collection from the snapshot bucket,
# mounted read-only at /snapshots. The doc pipeline names each snapshot
# <collection>.<UTC time>.snapshot after an ingest, so, sorted, the last file of a collection is
# its newest. Qdrant restores what it is given with --snapshot before it serves, and the image's
# own entrypoint starts it as it always did.
#
# SNAPSHOT_DIR and QDRANT_ENTRYPOINT exist for the tests.
set -euo pipefail
export LC_ALL=C   # byte order: the timestamps sort as times

dir="${SNAPSHOT_DIR:-/snapshots}"
declare -A newest=()
shopt -s nullglob
for f in "$dir"/*.snapshot; do
  name="$(basename "$f")"
  newest["${name%%.*}"]="$f"
done
args=()
for collection in "${!newest[@]}"; do
  args+=(--snapshot "${newest[$collection]}:$collection")
done
echo "qdrant-restore: restoring ${#newest[@]} collection(s) from ${dir}" >&2
exec "${QDRANT_ENTRYPOINT:-./entrypoint.sh}" ${args[@]+"${args[@]}"}
