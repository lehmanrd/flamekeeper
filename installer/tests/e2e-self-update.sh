#!/usr/bin/env bash
# End-to-end test of updating FlameKeeper from the UI, on a fresh Ubuntu 24.04 host in a
# privileged container (see e2e-clean-host.sh). Uses the internet: it installs this
# checkout as version 0.0.1, then updates through the API to the latest real release
# from GitHub and GHCR.
#
#   installer/tests/e2e-self-update.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
image=flamekeeper:e2e-0.0.1
name=fk-self-update-host
work="$(mktemp -d)"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm fk-self-update-docker fk-self-update-containerd >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

docker build -q --build-arg VERSION=0.0.1 -t "$image" "$root" >/dev/null
OUT_DIR="$work" "$root/installer/build.sh" lehmanrd/flamekeeper 0.0.1 >/dev/null
docker build -q -t fk-clean-host "$here/clean-host" >/dev/null
docker run -d --name "$name" --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock \
  -v fk-self-update-docker:/var/lib/docker -v fk-self-update-containerd:/var/lib/containerd \
  fk-clean-host >/dev/null
until docker exec "$name" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded'; do sleep 1; done
docker cp "$work/install.sh" "$name:/home/gamer/install.sh"
on_host() { docker exec -u gamer -w /home/gamer "$name" bash -c "$*"; }

echo "== install 0.0.1 (Docker first, then the local image)"
on_host "bash install.sh --yes --no-firewall --image $image" >/dev/null 2>&1 || true
docker save "$image" | docker exec -i "$name" docker load -q >/dev/null
on_host "FLAMEKEEPER_ADMIN_PASSWORD=e2e-password bash install.sh --yes --no-firewall --image $image --admin-user keeper" | grep "admin account"
on_host 'sudo grep FLAMEKEEPER_CLI /opt/flamekeeper/docker-compose.yml'

api() { on_host "curl -fsS -b cj -c cj -H 'X-FlameKeeper: 1' -H 'Content-Type: application/json' $*"; }
api "-d '{\"username\": \"keeper\", \"password\": \"e2e-password\"}' localhost:8080/api/auth/login" >/dev/null
status="$(api -X POST localhost:8080/api/system/update/check)"
echo "   check: $status"
latest="$(python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["update_available"] and s["can_update"], s; print(s["latest"]["version"])' "$status")"

echo "== update to $latest from the UI's API"
api "-d '{\"version\": \"$latest\"}' localhost:8080/api/system/update" >/dev/null
for _ in $(seq 1 120); do
  version="$(api localhost:8080/api/system 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || true)"
  [[ "$version" == "$latest" ]] && break
  sleep 3
done
[[ "$version" == "$latest" ]] || { echo "controller did not come back as $latest"; on_host 'sudo cat /srv/flamekeeper/self-update/update.log' || true; exit 1; }
echo "   controller now reports $version"
on_host 'sudo grep FLAMEKEEPER_IMAGE /opt/flamekeeper/.env'
on_host 'sudo docker inspect flamekeeper --format "   running image {{.Config.Image}}"'
on_host 'sudo tail -4 /srv/flamekeeper/self-update/update.log'
echo "== PASS"
