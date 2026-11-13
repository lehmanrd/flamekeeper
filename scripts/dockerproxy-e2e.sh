#!/usr/bin/env bash
# Runs a FlameKeeper controller that reaches Docker only through fk-dockerproxy, the
# way the installer will set it up (docs/socket-proxy.md).
#
#   scripts/dockerproxy-e2e.sh up IMAGE DATA_DIR [audit|enforce] [UI_PORT]
#   scripts/dockerproxy-e2e.sh logs          # what the proxy denied / would deny
#   scripts/dockerproxy-e2e.sh down          # removes both containers and the socket volume
#
# Import grants go in DATA_DIR-proxy-state/import.json (mounted read-only into the
# proxy only).
set -euo pipefail

PROXY=fk-e2e-dockerproxy
CTRL=fk-e2e-controller
VOL=fk-e2e-sock

case "${1:-}" in
  up)
    image=${2:?image}; data=${3:?data dir}; mode=${4:-audit}; port=${5:-18080}
    mkdir -p "$data" "$data-proxy-state"
    docker volume create "$VOL" >/dev/null
    docker run -d --name "$PROXY" --restart unless-stopped \
      --network none --read-only --security-opt no-new-privileges \
      --cap-drop ALL --cap-add DAC_READ_SEARCH \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$VOL":/run/fk \
      -v "$data":"$data":ro \
      -v "$data-proxy-state":/var/lib/fk-dockerproxy:ro \
      -e DATA_DIR="$data" -e FK_PROXY_MODE="$mode" -e FK_PROXY_LOG_ALLOWED=1 \
      "$image" fk-dockerproxy >/dev/null
    for _ in $(seq 20); do
      docker run --rm -v "$VOL":/run/fk "$image" test -S /run/fk/docker.sock && break
      sleep 0.5
    done
    docker run -d --name "$CTRL" --restart unless-stopped \
      --label flamekeeper.role=controller \
      -p "127.0.0.1:$port:8080" \
      -v "$VOL":/run/fk \
      -v "$data":"$data" \
      -e DATA_DIR="$data" -e TZ=Etc/UTC -e DOCKER_HOST=unix:///run/fk/docker.sock \
      "$image" >/dev/null
    echo "controller on http://127.0.0.1:$port (proxy mode: $mode)"
    ;;
  logs)
    docker logs "$PROXY" 2>&1 | grep -E '"msg":"(deny|would-deny|fatal)"' || echo "nothing denied"
    ;;
  down)
    docker rm -f "$CTRL" "$PROXY" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
    ;;
  *)
    sed -n '2,11p' "$0"; exit 2 ;;
esac
