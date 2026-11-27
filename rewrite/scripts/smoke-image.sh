#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" || "$1" == -* ]]; then
  printf 'Usage: bun run smoke:image <local-image>\n' >&2
  exit 2
fi

scripts="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# No host credentials/data, no external network, no persistent Docker volume.
docker run --rm --pull never --init \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /app/data:rw,nosuid,nodev,noexec,uid=1000,gid=1000,mode=0700 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777 \
  --mount "type=bind,source=$scripts,target=/app/scripts,readonly" \
  --entrypoint bun "$1" /app/scripts/smoke-runtime.ts
