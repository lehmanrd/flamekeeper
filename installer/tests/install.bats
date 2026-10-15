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
    --image my/img:dev --no-docker-install --no-firewall
  [ "$OPT_YES" = 1 ]
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
