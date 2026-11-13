#!/usr/bin/env bats
# Unit tests for installer/install.sh and installer/flamekeeper.
#   docker run --rm -v "$PWD":/code -w /code bats/bats:latest installer/tests

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  TMP="$(mktemp -d)"
  export FK_TESTING=1 FK_INSTALL_DIR="$TMP/opt" FK_OS_RELEASE="$TMP/os-release"
  # shellcheck source=../install.sh
  source "$ROOT/installer/install.sh"
}

teardown() {
  rm -rf "$TMP"
}

os_release() {
  printf 'ID=%s\nID_LIKE="%s"\nPRETTY_NAME="%s"\n' "$1" "$2" "$3" >"$FK_OS_RELEASE"
}

@test "parse_args accepts every option" {
  parse_args --yes --version v1.2.3 --data-dir /data --port 9000 --bind 127.0.0.1 \
    --image my/img:dev --no-docker-install --no-firewall --admin-user keeper
  [ "$OPT_YES" = 1 ]
  [ "$OPT_ADMIN_USER" = keeper ]
  [ "$OPT_VERSION" = 1.2.3 ]
  [ "$OPT_DATA_DIR" = /data ]
  [ "$OPT_PORT" = 9000 ]
  [ "$OPT_BIND" = 127.0.0.1 ]
  [ "$OPT_IMAGE" = my/img:dev ]
  [ "$OPT_NO_DOCKER_INSTALL" = 1 ]
  [ "$OPT_NO_FIREWALL" = 1 ]
}

@test "parse_args rejects bad input" {
  run parse_args --port 70000
  [ "$status" -ne 0 ]
  [[ "$output" == *"--port must be"* ]]
  run parse_args --data-dir relative/path
  [ "$status" -ne 0 ]
  run parse_args --bogus
  [ "$status" -ne 0 ]
  [[ "$output" == *"Unknown option: --bogus"* ]]
}

@test "detect_os reads os-release" {
  os_release ubuntu debian "Ubuntu 24.04 LTS"
  detect_os
  [ "$OS_ID" = ubuntu ]
  [ "$OS_LIKE" = debian ]
  [ "$OS_NAME" = "Ubuntu 24.04 LTS" ]
}

@test "docker_install_method picks the right installer" {
  [ "$(docker_install_method ubuntu debian)" = get-docker ]
  [ "$(docker_install_method debian '')" = get-docker ]
  [ "$(docker_install_method fedora '')" = get-docker ]
  [ "$(docker_install_method rocky 'rhel centos fedora')" = get-docker ]
  [ "$(docker_install_method linuxmint 'ubuntu debian')" = get-docker ]
  [ "$(docker_install_method somederivative 'ubuntu')" = get-docker ]
  [ "$(docker_install_method arch '')" = pacman ]
  [ "$(docker_install_method endeavouros arch)" = pacman ]
  [ "$(docker_install_method opensuse-tumbleweed 'opensuse suse')" = zypper ]
  [ "$(docker_install_method gentoo '')" = unsupported ]
  [ "$(docker_install_method nixos '')" = unsupported ]
}

@test "version_ge compares versions" {
  version_ge 24.0.7 20.10
  version_ge 20.10 20.10
  version_ge 29.6.1 20.10
  ! version_ge 19.03.15 20.10
  ! version_ge 0 20.10
}

@test "fresh install uses defaults" {
  OPT_VERSION=""
  detect_tz() { echo Europe/Berlin; }
  resolve_settings
  [ "$FK_PORT" = 8080 ]
  [ "$FK_BIND" = 0.0.0.0 ]
  [ "$FK_DATA_DIR" = /srv/flamekeeper ]
  [ "$FK_TZ" = Europe/Berlin ]
  [ "$FK_IMAGE" = flamekeeper:dev ]   # unreleased checkout
}

@test "re-run keeps existing settings; flags override them" {
  mkdir -p "$FK_INSTALL_DIR"
  cat >"$FK_INSTALL_DIR/.env" <<EOF
FLAMEKEEPER_IMAGE=ghcr.io/x/flamekeeper:0.0.9
FLAMEKEEPER_PORT=9090
FLAMEKEEPER_BIND=127.0.0.1
DATA_DIR=/mnt/games
TZ=America/New_York
BASE_PATH=/fk
EOF
  resolve_settings
  [ "$FK_PORT" = 9090 ]
  [ "$FK_BIND" = 127.0.0.1 ]
  [ "$FK_DATA_DIR" = /mnt/games ]
  [ "$FK_TZ" = America/New_York ]
  [ "$FK_BASE_PATH" = /fk ]

  OPT_PORT=7000
  resolve_settings
  [ "$FK_PORT" = 7000 ]
  [ "$FK_DATA_DIR" = /mnt/games ]
}

@test "release builds point the image at GHCR with the pinned version" {
  FK_REPO=someone/flamekeeper
  FK_RELEASE=0.3.1
  resolve_settings
  [ "$FK_IMAGE" = ghcr.io/someone/flamekeeper:0.3.1 ]
  OPT_VERSION=0.2.0
  resolve_settings
  [ "$FK_IMAGE" = ghcr.io/someone/flamekeeper:0.2.0 ]
  OPT_IMAGE=local/fk:test
  resolve_settings
  [ "$FK_IMAGE" = local/fk:test ]
  OPT_IMAGE="" OPT_VERSION="" FK_REPO=SomeOne/FlameKeeper
  resolve_settings
  [ "$FK_IMAGE" = ghcr.io/someone/flamekeeper:0.3.1 ]
}

@test "render_env round-trips through env_get" {
  FK_IMAGE=img:1 FK_PORT=8081 FK_BIND=0.0.0.0 FK_DATA_DIR=/srv/fk FK_TZ=Etc/UTC FK_BASE_PATH="" DOCKER_SOCK=/run/user/1000/docker.sock
  render_env >"$TMP/.env"
  [ "$(env_get "$TMP/.env" FLAMEKEEPER_PORT)" = 8081 ]
  [ "$(env_get "$TMP/.env" DOCKER_SOCK)" = /run/user/1000/docker.sock ]
  [ "$(env_get "$TMP/.env" BASE_PATH)" = "" ]
  [ "$(env_get "$TMP/missing" X)" = "" ]
}

@test "health_setup_error extracts the problem" {
  [ "$(health_setup_error '{"status":"ok","app":"FlameKeeper","setup_error":null}')" = "" ]
  [ "$(health_setup_error '{"status":"ok","setup_error":"DATA_DIR=/x is not mounted"}')" = "DATA_DIR=/x is not mounted" ]
}

@test "confirm: --yes answers yes; no terminal and no --yes fails clearly" {
  OPT_YES=1
  confirm "Proceed?"
  OPT_YES=0
  run bash -c "FK_TESTING=1 FK_OS_RELEASE=/dev/null source '$ROOT/installer/install.sh'; confirm 'Proceed?' </dev/null" </dev/null
  if { : </dev/tty; } 2>/dev/null; then
    skip "a terminal is attached"
  fi
  [ "$status" -ne 0 ]
  [[ "$output" == *"Re-run with --yes"* ]]
}

@test "checkout install reads the CLI and compose files next to the script" {
  run embedded_or_file "$EMBEDDED_CLI" flamekeeper
  [ "$status" -eq 0 ]
  [[ "${lines[0]}" == "#!/usr/bin/env bash" ]]
  run embedded_or_file "$EMBEDDED_COMPOSE" compose.template.yml
  [[ "$output" == *'${DATA_DIR}:${DATA_DIR}'* ]]
}

@test "release build embeds everything and runs nothing when truncated" {
  OUT_DIR="$TMP/dist" bash "$ROOT/installer/build.sh" someone/flamekeeper v0.4.0 >/dev/null
  built="$TMP/dist/install.sh"
  grep -q '^FK_REPO="someone/flamekeeper" ' "$built"
  grep -q '^FK_RELEASE="0.4.0" ' "$built"
  ! grep -qxE '__FK_EMBED_(CLI|COMPOSE)__' "$built"
  (cd "$TMP/dist" && sha256sum -c install.sh.sha256)

  # The embedded copies match the sources (with the repo filled in).
  (
    source "$built"
    [ "$EMBEDDED_CLI" = "$(sed 's#__FK_REPO__#someone/flamekeeper#g' "$ROOT/installer/flamekeeper")" ]
    [ "$EMBEDDED_COMPOSE" = "$(cat "$ROOT/installer/compose.template.yml")" ]
  )

  # Cut the script anywhere before the end: bash parses but never calls main.
  total=$(wc -l <"$built")
  for cut in 50 200 $((total - 3)); do
    head -n "$cut" "$built" >"$TMP/cut.sh"
    FK_TESTING=0 run bash "$TMP/cut.sh" --help
    [[ "$output" != *"FlameKeeper installer"* ]]
  done
}

@test "flamekeeper CLI: help, version and unknown commands" {
  mkdir -p "$FK_INSTALL_DIR"
  echo "FLAMEKEEPER_IMAGE=ghcr.io/x/flamekeeper:0.5.2" >"$FK_INSTALL_DIR/.env"
  run bash "$ROOT/installer/flamekeeper" help
  [ "$status" -eq 0 ]
  [[ "$output" == *"uninstall [--purge]"* ]]
  run bash "$ROOT/installer/flamekeeper" version
  [ "$output" = 0.5.2 ]
  run bash "$ROOT/installer/flamekeeper" frobnicate
  [ "$status" -eq 2 ]
}

@test "write_files tells the controller where the flamekeeper command is" {
  FK_BIN_DIR="$TMP/bin" FK_DATA_DIR="$TMP/data" DOCKER_SOCK=/var/run/docker.sock
  mkdir -p "$FK_BIN_DIR"
  write_files >/dev/null
  grep -qx "      - FLAMEKEEPER_CLI=$TMP/bin/flamekeeper" "$FK_INSTALL_DIR/docker-compose.yml"
  ! grep -q "__FK_" "$FK_INSTALL_DIR/docker-compose.yml"
  [ -x "$FK_BIN_DIR/flamekeeper" ]
}

@test "flamekeeper update passes --version and --no-firewall to the installer" {
  sed -e 's#__FK_REPO__#someone/flamekeeper#' "$ROOT/installer/flamekeeper" >"$TMP/fk"
  mkdir -p "$TMP/stub"
  # A fake curl that "downloads" an installer printing its arguments.
  cat >"$TMP/stub/curl" <<'STUB'
#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in -o) out="$2"; shift ;; https://*) echo "url=$1" >&2 ;; esac
  shift
done
printf 'echo "installer args: $*"\n' >"$out"
STUB
  chmod +x "$TMP/stub/curl"
  PATH="$TMP/stub:$PATH" run bash "$TMP/fk" update --version v0.5.0 --no-firewall
  [ "$status" -eq 0 ]
  [[ "$output" == *"url=https://github.com/someone/flamekeeper/releases/download/v0.5.0/install.sh"* ]]
  [[ "$output" == *"installer args: --yes --no-firewall --version 0.5.0"* ]]
  PATH="$TMP/stub:$PATH" run bash "$TMP/fk" update
  [[ "$output" == *"url=https://github.com/someone/flamekeeper/releases/latest/download/install.sh"* ]]
  [[ "$output" == *"installer args: --yes" ]]
  run bash "$TMP/fk" update --frob
  [ "$status" -ne 0 ]
}

# A host for `flamekeeper update-request`: .env, a request folder, and a fake curl whose
# "installer" prints its arguments (and fails if FAIL_INSTALL is set).
update_request_host() {
  sed -e 's#__FK_REPO__#someone/flamekeeper#' "$ROOT/installer/flamekeeper" >"$TMP/fk"
  mkdir -p "$FK_INSTALL_DIR" "$TMP/data/self-update" "$TMP/stub"
  printf 'FLAMEKEEPER_IMAGE=ghcr.io/someone/flamekeeper:0.4.1\nDATA_DIR=%s\n' "$TMP/data" \
    >"$FK_INSTALL_DIR/.env"
  cat >"$TMP/stub/curl" <<'STUB'
#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in -o) out="$2"; shift ;; https://*) echo "url=$1" >&2 ;; esac
  shift
done
printf 'echo "installer args: $*"; [[ -z "${FAIL_INSTALL:-}" ]]\n' >"$out"
STUB
  chmod +x "$TMP/stub/curl"
}

update_request() { PATH="$TMP/stub:$PATH" run bash "$TMP/fk" update-request; }
status_json() { cat "$FK_INSTALL_DIR/self-update/status.json"; }

@test "update-request runs the update and reports to the install folder" {
  [ "$(id -u)" -eq 0 ] || skip "needs root"
  update_request_host
  echo "0.5.0 0123456789abcdef" >"$TMP/data/self-update/request"
  update_request
  [ "$status" -eq 0 ]
  [ ! -e "$TMP/data/self-update/request" ]
  grep -q "releases/download/v0.5.0/install.sh" "$FK_INSTALL_DIR/self-update/update.log"
  grep -q "installer args: --yes --no-firewall --version 0.5.0" "$FK_INSTALL_DIR/self-update/update.log"
  [[ "$(status_json)" == *'"id": "0123456789abcdef"'*'"state": "succeeded", "exit_code": 0'* ]]
  # Nothing is written back into DATA_DIR, where the controller could plant symlinks.
  [ -z "$(ls -A "$TMP/data/self-update")" ]

  echo "0.5.0 0123456789abcdef" >"$TMP/data/self-update/request"
  export FAIL_INSTALL=1
  update_request
  [ "$status" -ne 0 ]
  [[ "$(status_json)" == *'"state": "failed", "exit_code": 1'* ]]
}

@test "update-request treats the request as untrusted" {
  [ "$(id -u)" -eq 0 ] || skip "needs root"
  update_request_host
  local req="$TMP/data/self-update/request" bad
  for bad in "0.5.0" "0.5.0 xyz" "0.5.0; rm -rf / 0123456789abcdef" '$(id) 0123456789abcdef' \
    "0.5.0 0123456789abcdef extra" "../0.5.0 0123456789abcdef" "v0.5.0 0123456789abcdef"; do
    printf '%s\n' "$bad" >"$req"
    update_request
    [ "$status" -ne 0 ]
    [[ "$output" == *"malformed"* ]]
    [ ! -e "$req" ]
    [ ! -e "$FK_INSTALL_DIR/self-update/status.json" ]
  done

  # Not newer than what's installed: refused, and the controller is told why.
  echo "0.4.1 0123456789abcdef" >"$req"
  update_request
  [ "$status" -ne 0 ]
  [[ "$(status_json)" == *'"state": "failed"'*"isn't newer than the installed 0.4.1"* ]]
  echo "0.3.9 0123456789abcdef" >"$req"
  update_request
  [ "$status" -ne 0 ]

  # A symlink or a FIFO instead of a file: removed, never read.
  echo "0.5.0 0123456789abcdef" >"$TMP/elsewhere"
  ln -s "$TMP/elsewhere" "$req"
  update_request
  [ "$status" -ne 0 ] && [ ! -e "$req" ] && [ ! -L "$req" ] && [ -e "$TMP/elsewhere" ]
  mkfifo "$req"
  update_request
  [ "$status" -ne 0 ] && [ ! -e "$req" ]

  # A symlinked request folder (pointing the rm elsewhere) is refused.
  rm -r "$TMP/data/self-update"
  mkdir "$TMP/other"
  echo "0.5.0 0123456789abcdef" >"$TMP/other/request"
  ln -s "$TMP/other" "$TMP/data/self-update"
  update_request
  [ "$status" -ne 0 ] && [[ "$output" == *"isn't a plain folder"* ]] && [ -e "$TMP/other/request" ]
}

@test "install_updater installs the systemd units, or explains without systemd" {
  FK_BIN_DIR="$TMP/bin" FK_DATA_DIR="$TMP/data" FK_SYSTEMD_DIR="$TMP/systemd"
  mkdir -p "$TMP/stub" "$FK_INSTALL_DIR/self-update"
  printf '#!/usr/bin/env bash\necho "systemctl $*" >>"%s/systemctl.log"\n' "$TMP" >"$TMP/stub/systemctl"
  chmod +x "$TMP/stub/systemctl"
  PATH="$TMP/stub:$PATH"
  has_systemd() { true; }
  install_updater >/dev/null
  grep -qx "PathExists=$TMP/data/self-update/request" "$TMP/systemd/flamekeeper-update.path"
  grep -qx "ExecStart=$TMP/bin/flamekeeper update-request" "$TMP/systemd/flamekeeper-update.service"
  grep -qx "Type=oneshot" "$TMP/systemd/flamekeeper-update.service"
  grep -q "systemctl enable --now flamekeeper-update.path" "$TMP/systemctl.log"
  [ "$(cat "$FK_INSTALL_DIR/self-update/updater")" = systemd ]

  has_systemd() { false; }
  run install_updater
  [ "$status" -eq 0 ] && [[ "$output" == *"sudo flamekeeper update"* ]]
  [ ! -e "$FK_INSTALL_DIR/self-update/updater" ]
}

@test "write_files mounts the host's update folder read-only into the controller" {
  FK_BIN_DIR="$TMP/bin" FK_DATA_DIR="$TMP/data" DOCKER_SOCK=/var/run/docker.sock
  mkdir -p "$FK_BIN_DIR"
  write_files >/dev/null
  grep -qx "      - $FK_INSTALL_DIR/self-update:/var/lib/flamekeeper/host-update:ro" \
    "$FK_INSTALL_DIR/docker-compose.yml"
  [ -d "$FK_INSTALL_DIR/self-update" ]
}

# A host for `flamekeeper import`: an install folder and a fake docker that knows an old
# server container and one of FlameKeeper's own.
import_host() {
  sed -e 's#__FK_REPO__#someone/flamekeeper#' "$ROOT/installer/flamekeeper" >"$TMP/fk"
  mkdir -p "$FK_INSTALL_DIR" "$TMP/stub" "$TMP/old-server"
  touch "$FK_INSTALL_DIR/docker-compose.yml"
  cat >"$TMP/stub/docker" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == info ]] && exit 0
name="${*: -1}"
case "$name" in old-server | flamekeeper-embervale) ;; *) exit 1 ;; esac
if [[ "$*" == *--format* ]]; then
  [[ "$name" == flamekeeper-embervale ]] && echo true || echo ""
fi
STUB
  chmod +x "$TMP/stub/docker"
}

fk_import() { PATH="$TMP/stub:$PATH" run bash "$TMP/fk" import "$@"; }
grant() { cat "$FK_INSTALL_DIR/proxy/import.json"; }

@test "flamekeeper import --allow writes a one-hour grant for the proxy" {
  [ "$(id -u)" -eq 0 ] || skip "needs root"
  import_host
  fk_import
  [ "$status" -eq 0 ] && [[ "$output" == *"may not import from anything"* ]]

  fk_import --allow "$TMP/old-server" old-server
  [ "$status" -eq 0 ]
  [[ "$(grant)" =~ ^\{\"until\":\ [0-9]+,\ \"paths\":\ \[\"$TMP/old-server\"\],\ \"containers\":\ \[\"old-server\"\]\}$ ]]
  local until
  until="$(grant | sed 's/.*"until": \([0-9]*\).*/\1/')"
  [ $((until - $(date +%s))) -gt 3500 ] && [ $((until - $(date +%s))) -le 3600 ]
  fk_import
  [[ "$output" == *"folder     $TMP/old-server"* && "$output" == *"container  old-server"* ]]

  # A symlink is resolved: the proxy compares the real folder.
  ln -s "$TMP/old-server" "$TMP/link"
  fk_import --allow "$TMP/link"
  [ "$status" -eq 0 ] && [[ "$output" == *"enter $TMP/old-server in FlameKeeper"* ]]
  [[ "$(grant)" == *"\"paths\": [\"$TMP/old-server\"], \"containers\": []"* ]]

  fk_import --done
  [ "$status" -eq 0 ] && [ ! -e "$FK_INSTALL_DIR/proxy/import.json" ]
}

@test "flamekeeper import refuses what it shouldn't allow" {
  [ "$(id -u)" -eq 0 ] || skip "needs root"
  import_host
  mkdir -p "$TMP/we\"ird"
  local bad
  for bad in / "$TMP/nope" "$TMP/we\"ird" "$FK_INSTALL_DIR/docker-compose.yml" \
    flamekeeper-embervale no-such-container '$(id)' "-rf"; do
    fk_import --allow "$bad"
    [ "$status" -ne 0 ]
  done
  [ ! -e "$FK_INSTALL_DIR/proxy/import.json" ]
  fk_import --allow
  [ "$status" -ne 0 ]
}

@test "the controller reaches Docker only through the proxy" {
  FK_BIN_DIR="$TMP/bin" FK_DATA_DIR="$TMP/data" DOCKER_SOCK=/var/run/docker.sock
  mkdir -p "$FK_BIN_DIR"
  write_files >/dev/null
  local compose="$FK_INSTALL_DIR/docker-compose.yml" controller
  # The socket is mounted once, in the dockerproxy service.
  [ "$(grep -c ":/var/run/docker.sock" "$compose")" -eq 1 ]
  controller="$(sed -n '/^  flamekeeper:/,$p' "$compose")"
  [[ "$controller" != *"/var/run/docker.sock"* ]]
  [[ "$controller" == *"DOCKER_HOST=unix:///run/fk/docker.sock"* ]]
  [[ "$controller" == *"flamekeeper.role: controller"* ]]
  grep -q "      - $FK_INSTALL_DIR/proxy:/var/lib/fk-dockerproxy:ro" "$compose"
  [ -d "$FK_INSTALL_DIR/proxy" ]
}

@test "the proxy mode defaults to enforce and keeps an admin's audit" {
  resolve_settings
  [ "$FK_PROXY_MODE" = enforce ]
  render_env | grep -qx "FK_PROXY_MODE=enforce"
  mkdir -p "$FK_INSTALL_DIR"
  echo "FK_PROXY_MODE=audit" >"$FK_INSTALL_DIR/.env"
  resolve_settings
  [ "$FK_PROXY_MODE" = audit ]
  echo "FK_PROXY_MODE=off" >"$FK_INSTALL_DIR/.env"
  resolve_settings
  [ "$FK_PROXY_MODE" = enforce ]
}
