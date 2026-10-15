#!/usr/bin/env bash
# End-to-end installer test on a fresh Ubuntu 24.04 (systemd, no Docker) running in a
# privileged container. The installer installs Docker inside it, then FlameKeeper.
#
#   installer/tests/e2e-clean-host.sh [controller-image]   (default flamekeeper:dev)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
image="${1:-flamekeeper:dev}"
name=fk-clean-host
work="$(mktemp -d)"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm fk-clean-host-docker fk-clean-host-containerd >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

OUT_DIR="$work" "$root/installer/build.sh" example/flamekeeper 0.1.0 >/dev/null
docker build -q -t fk-clean-host "$here/clean-host" >/dev/null
docker run -d --name "$name" --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock \
  -v fk-clean-host-docker:/var/lib/docker -v fk-clean-host-containerd:/var/lib/containerd \
  fk-clean-host >/dev/null
until docker exec "$name" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded'; do sleep 1; done
docker cp "$work/install.sh" "$name:/home/gamer/install.sh"
on_host() { docker exec -u gamer -w /home/gamer "$name" bash -c "$*"; }

echo "== fresh host: docker present? $(on_host 'command -v docker || echo no')"
echo "== run 1: curl | bash as a sudo user (installs Docker)"
# The controller image isn't in any registry here, so this run installs Docker and then
# stops at "docker compose up". The local image is then loaded into the new Docker.
on_host "curl -fsSL file:///home/gamer/install.sh | bash -s -- --yes --no-firewall --image $image" \
  && { echo "unexpected: install succeeded without the image"; exit 1; } \
  || echo "   (expected failure: $image not in the fresh Docker yet)"
on_host 'docker --version; sudo docker compose version'
docker save "$image" | docker exec -i "$name" docker load -q

echo "== run 2: install with the local image (--yes: no admin prompt)"
out="$(on_host "curl -fsSL file:///home/gamer/install.sh | bash -s -- --yes --no-firewall --image $image --port 8088")"
printf '%s\n' "$out" | tail -12
grep -q "within 10 minutes to create the admin account" <<<"$out" || { echo "missing setup hint"; exit 1; }
on_host 'sudo cat /opt/flamekeeper/.env'
on_host 'curl -fsS localhost:8088/api/health; echo'

echo "== run 3: re-run keeps settings (port 8088 without --port), creates the admin unattended"
on_host "curl -fsSL file:///home/gamer/install.sh | FLAMEKEEPER_ADMIN_PASSWORD=e2e-password bash -s -- --yes --no-firewall --image $image --admin-user keeper" | grep "admin account"
on_host 'sudo grep FLAMEKEEPER_PORT /opt/flamekeeper/.env'
on_host 'curl -fsS localhost:8088/api/health >/dev/null && echo "   health ok on 8088"'

echo "== CLI"
on_host 'flamekeeper status'
on_host 'flamekeeper doctor'
on_host "curl -fsS -H 'X-FlameKeeper: 1' -H 'Content-Type: application/json' \
  -d '{\"username\": \"keeper\", \"password\": \"e2e-password\"}' localhost:8088/api/auth/login; echo"
on_host "curl -fsSL file:///home/gamer/install.sh | bash -s -- --yes --no-firewall --image $image" \
  | grep -q "within 10 minutes" && { echo "unexpected: setup hint after the admin exists"; exit 1; }
echo "   upgrade with an admin: no setup hint"
on_host 'sudo flamekeeper uninstall'
on_host 'test ! -e /opt/flamekeeper && test ! -e /usr/local/bin/flamekeeper && sudo test -d /srv/flamekeeper && echo "   uninstall removed controller, kept data"'
echo "== PASS"
