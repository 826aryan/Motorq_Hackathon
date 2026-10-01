#!/usr/bin/env bash
# Runs ON the VM (called by .github/workflows/deploy.yml): unpack, pull the new images, restart, smoke test,
# and roll back to the previously deployed tag if the smoke test fails.
set -euo pipefail
REGISTRY="$1"; TAG="$2"
cd ~/asset-recovery
tar xzf /tmp/bundle.tgz
PREV_TAG=$(cat .deployed_tag 2>/dev/null || echo "")
compose() { REGISTRY="$REGISTRY" IMAGE_TAG="$1" docker compose -f infra/docker-compose.yml --env-file .env "${@:2}"; }

if [ ! -f data/graph/road_graph.npz ]; then                    # first deploy: build the road graph once
  compose "$TAG" --profile tools run --rm graph-builder
fi

compose "$TAG" pull --quiet
compose "$TAG" up -d --no-build --remove-orphans

smoke() {
  for i in $(seq 1 30); do
    if curl -fsS localhost:8080/health >/dev/null && curl -fsS localhost:8080/api/health | grep -q '"ok"'; then
      return 0
    fi
    sleep 5
  done
  return 1
}

if smoke; then
  echo "$TAG" > .deployed_tag
  echo "deployed $TAG"
else
  echo "smoke test failed for $TAG"
  if [ -n "$PREV_TAG" ]; then
    echo "rolling back to $PREV_TAG"
    compose "$PREV_TAG" up -d --no-build --remove-orphans
  fi
  exit 1
fi
