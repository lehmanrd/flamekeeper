#!/usr/bin/env bash
# FlameKeeper installer: Enshrouded dedicated server manager.
#
#   curl -fsSL https://github.com/__FK_REPO__/releases/latest/download/install.sh | bash
#
# Installs Docker if it is missing (after asking), writes /opt/flamekeeper, starts the
# FlameKeeper container and the `flamekeeper` command. Safe to re-run: it upgrades in
# place and keeps your settings. Options (pass after `bash -s --`):
#
#   --yes                 Don't ask; accept defaults (needed when there is no terminal)
#   --version <v>         Install a specific release (default: this installer's release)
#   --data-dir <path>     Where worlds, saves and backups live (default /srv/flamekeeper)
#   --port <n>            Web UI port (default 8080)
#   --bind <ip>           Web UI bind address (default 0.0.0.0; 127.0.0.1 = local only)
#   --no-docker-install   Fail instead of installing Docker
#   --no-firewall         Don't touch ufw/firewalld
#   --image <ref>         Use this controller image instead (development)
#
# Everything runs inside main(), called on the last line, so a partially downloaded
# script never runs.

set -euo pipefail

FK_REPO="__FK_REPO__"          # set by the release build
FK_RELEASE="__FK_VERSION__"    # set by the release build
FK_INSTALL_DIR="${FK_INSTALL_DIR:-/opt/flamekeeper}"
FK_BIN_DIR="${FK_BIN_DIR:-/usr/local/bin}"
FK_OS_RELEASE="${FK_OS_RELEASE:-/etc/os-release}"
MIN_DOCKER="20.10"
GAME_PORTS="15637:15646"

# Filled in by the release build from installer/flamekeeper and compose.template.yml.
EMBEDDED_CLI=$(cat <<'FK_EMBED_CLI'
__FK_EMBED_CLI__
FK_EMBED_CLI
)
EMBEDDED_COMPOSE=$(cat <<'FK_EMBED_COMPOSE'
__FK_EMBED_COMPOSE__
FK_EMBED_COMPOSE
)

# --- output -------------------------------------------------------------------------

if [[ -t 1 ]]; then
  C_RESET=$'\e[0m' C_BOLD=$'\e[1m' C_DIM=$'\e[2m' C_RED=$'\e[31m' C_YEL=$'\e[33m' C_GRN=$'\e[32m' C_GOLD=$'\e[38;5;179m'
else
  C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_YEL="" C_GRN="" C_GOLD=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$C_GOLD" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    %s✔%s %s\n' "$C_GRN" "$C_RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YEL" "$C_RESET" "$*" >&2; }
die() {
  printf '\n%sError:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
  exit 1
}

# --- options ------------------------------------------------------------------------

OPT_YES="${FK_YES:-0}"
OPT_VERSION=""
OPT_DATA_DIR=""
OPT_PORT=""
OPT_BIND=""
OPT_IMAGE=""
OPT_NO_DOCKER_INSTALL=0
OPT_NO_FIREWALL=0

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' ||
    echo "Usage: install.sh [--yes] [--version v] [--data-dir path] [--port n] [--bind ip]"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y | --yes) OPT_YES=1 ;;
      --version) OPT_VERSION="${2:?--version needs a value}"; shift ;;
      --data-dir) OPT_DATA_DIR="${2:?--data-dir needs a value}"; shift ;;
      --port) OPT_PORT="${2:?--port needs a value}"; shift ;;
      --bind) OPT_BIND="${2:?--bind needs a value}"; shift ;;
      --image) OPT_IMAGE="${2:?--image needs a value}"; shift ;;
      --no-docker-install) OPT_NO_DOCKER_INSTALL=1 ;;
      --no-firewall) OPT_NO_FIREWALL=1 ;;
      -h | --help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
  done
  if [[ -n "$OPT_PORT" ]] && ! [[ "$OPT_PORT" =~ ^[0-9]+$ && "$OPT_PORT" -ge 1 && "$OPT_PORT" -le 65535 ]]; then
    die "--port must be a number between 1 and 65535"
  fi
  if [[ -n "$OPT_DATA_DIR" && "$OPT_DATA_DIR" != /* ]]; then
    die "--data-dir must be an absolute path"
  fi
  if [[ -n "$OPT_VERSION" ]]; then
    OPT_VERSION="${OPT_VERSION#v}"
  fi
}

# --- prompts ------------------------------------------------------------------------

# confirm "Question?" [default Y|N]. Reads the terminal, not stdin (stdin is the script
# when piped from curl).
confirm() {
  local question="$1" default="${2:-Y}" reply hint="[Y/n]"
  [[ "$default" == "N" ]] && hint="[y/N]"
  if [[ "$OPT_YES" == 1 ]]; then
    return 0
  fi
  if ! { : </dev/tty; } 2>/dev/null; then
    die "No terminal to ask \"$question\". Re-run with --yes to accept defaults."
  fi
  printf '    %s %s ' "$question" "$hint" >/dev/tty
  read -r reply </dev/tty || reply=""
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

# --- privileges -----------------------------------------------------------------------

SUDO=""
setup_privileges() {
  if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
    if sudo -n true 2>/dev/null; then
      return
    fi
    { : </dev/tty; } 2>/dev/null || die "sudo needs a password but there is no terminal. Run as root instead."
    info "Some steps need root; sudo will ask for your password."
    # shellcheck disable=SC2024  # the redirect is meant to run as the user
    sudo -v </dev/tty || die "sudo failed"
  else
    die "Run this installer as root (or install sudo)."
  fi
}

as_root() {
  if [[ -n "$SUDO" ]]; then
    $SUDO "$@"
  else
    "$@"
  fi
}

# --- host detection -------------------------------------------------------------------

OS_ID=""
OS_LIKE=""
OS_NAME=""
detect_os() {
  [[ -r "$FK_OS_RELEASE" ]] || die "Cannot read $FK_OS_RELEASE; unsupported Linux distribution."
  # shellcheck disable=SC1090
  OS_ID="$(. "$FK_OS_RELEASE" && echo "${ID:-}")"
  # shellcheck disable=SC1090
  OS_LIKE="$(. "$FK_OS_RELEASE" && echo "${ID_LIKE:-}")"
  # shellcheck disable=SC1090
  OS_NAME="$(. "$FK_OS_RELEASE" && echo "${PRETTY_NAME:-$ID}")"
}

# Prints how Docker should be installed on this distro:
# get-docker | pacman | zypper | unsupported
docker_install_method() {
  local id="$1" like="$2"
  case "$id" in
    ubuntu | debian | raspbian | fedora | centos | rhel | rocky | almalinux | ol | linuxmint | pop | elementary | zorin | kali | neon)
      echo get-docker; return ;;
    arch | manjaro | endeavouros | garuda) echo pacman; return ;;
    opensuse* | sles) echo zypper; return ;;
  esac
  case " $like " in
    *" ubuntu "* | *" debian "* | *" fedora "* | *" rhel "* | *" centos "*) echo get-docker ;;
    *" arch "*) echo pacman ;;
    *" suse "*) echo zypper ;;
    *) echo unsupported ;;
  esac
}

package_manager() {
  local pm
  for pm in apt-get dnf yum pacman zypper apk; do
    if command -v "$pm" >/dev/null 2>&1; then
      echo "$pm"
      return
    fi
  done
  echo none
}

pkg_install() {
  local pm
  pm="$(package_manager)"
  case "$pm" in
    apt-get) as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq &&
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf) as_root dnf install -y -q "$@" ;;
    yum) as_root yum install -y -q "$@" ;;
    pacman) as_root pacman -Sy --noconfirm --needed "$@" ;;
    zypper) as_root zypper --non-interactive install "$@" ;;
    *) return 1 ;;
  esac
}

# version_ge 24.0.7 20.10 → true
version_ge() {
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

has_systemd() {
  [[ -d /run/systemd/system ]]
}

check_platform() {
  step "Checking this machine"
  [[ "$(uname -s)" == "Linux" ]] || die "FlameKeeper runs on Linux only."
  local arch
  arch="$(uname -m)"
  if [[ "$arch" != "x86_64" ]]; then
    die "This machine is $arch. The Enshrouded server is x86-64 only, so FlameKeeper needs an x86_64 (amd64) host."
  fi
  detect_os
  ok "$OS_NAME ($arch)"
  if has_systemd; then
    ok "systemd"
  else
    warn "systemd not found: Docker must already be running, and won't be started automatically."
  fi
  if ! command -v curl >/dev/null 2>&1; then
    info "Installing curl"
    pkg_install curl ca-certificates || die "Please install curl and re-run."
  fi
}

# --- Docker ---------------------------------------------------------------------------

DOCKER_SOCK="/var/run/docker.sock"

docker_cmd() {
  # Use sudo for docker only when the current user can't reach the daemon.
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    as_root docker "$@"
  fi
}

install_docker() {
  local method
  method="$(docker_install_method "$OS_ID" "$OS_LIKE")"
  case "$method" in
    get-docker)
      info "Using Docker's official install script (get.docker.com)"
      curl -fsSL https://get.docker.com -o /tmp/flamekeeper-get-docker.sh
      as_root sh /tmp/flamekeeper-get-docker.sh
      rm -f /tmp/flamekeeper-get-docker.sh
      ;;
    pacman) pkg_install docker docker-compose ;;
    zypper) pkg_install docker docker-compose ;;
    *)
      die "Can't install Docker automatically on $OS_NAME. Install Docker Engine and the compose plugin (https://docs.docker.com/engine/install/), then re-run."
      ;;
  esac
}

ensure_docker() {
  step "Docker"
  if ! command -v docker >/dev/null 2>&1; then
    if [[ "$OPT_NO_DOCKER_INSTALL" == 1 ]]; then
      die "Docker is not installed (and --no-docker-install was given)."
    fi
    info "Docker is not installed. FlameKeeper runs every server in a Docker container."
    confirm "Install Docker now?" Y || die "Docker is required. Install it and re-run."
    install_docker
  fi

  if docker --version 2>/dev/null | grep -qi podman; then
    warn "This 'docker' is Podman. FlameKeeper works with Podman's Docker-compatible socket on a best-effort basis."
    confirm "Continue with Podman?" N || die "Stopped. Install Docker Engine for full support."
  fi

  if has_systemd && ! docker_cmd info >/dev/null 2>&1; then
    info "Starting the Docker service"
    as_root systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  docker_cmd info >/dev/null 2>&1 || die "Docker is installed but not running. Start it (systemctl start docker) and re-run."

  local version
  version="$(docker_cmd version --format '{{.Server.Version}}' 2>/dev/null || echo 0)"
  version_ge "$version" "$MIN_DOCKER" || die "Docker $version is too old; FlameKeeper needs $MIN_DOCKER or newer."
  ok "Docker $version"

  if ! docker_cmd compose version >/dev/null 2>&1; then
    info "Installing the Docker compose plugin"
    case "$(package_manager)" in
      apt-get | dnf | yum) pkg_install docker-compose-plugin ;;
      pacman | zypper) pkg_install docker-compose ;;
      *) false ;;
    esac || die "Couldn't install the compose plugin. Install 'docker compose' and re-run."
  fi
  ok "$(docker_cmd compose version 2>/dev/null | head -n1)"

  # Rootless Docker (or a remote context) uses a different socket.
  local host
  host="$(docker_cmd context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
  if [[ "$host" == unix://* ]]; then
    DOCKER_SOCK="${host#unix://}"
  fi
  if docker_cmd info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
    warn "Rootless Docker detected (socket $DOCKER_SOCK). This is supported on a best-effort basis."
  fi
}

# --- host guidance (warn, never block) ------------------------------------------------

nearest_existing_dir() {
  local d="$1"
  while [[ ! -d "$d" && "$d" != "/" ]]; do d="$(dirname "$d")"; done
  echo "$d"
}

check_resources() {
  local data_dir="$1" mem_kb mem_gb cores free_gb kernel
  step "Resources"
  mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  mem_gb=$((mem_kb / 1024 / 1024))
  cores="$(nproc 2>/dev/null || echo 0)"
  free_gb="$(df -Pk "$(nearest_existing_dir "$data_dir")" | awk 'NR==2 {print int($4/1024/1024)}')"
  kernel="$(uname -r)"
  if [[ "$mem_gb" -ge 15 ]]; then ok "${mem_gb} GB RAM"; else warn "${mem_gb} GB RAM: Keen recommends 16 GB per server."; fi
  if [[ "$cores" -ge 6 ]]; then ok "$cores CPU cores"; else warn "$cores CPU cores: Keen recommends 6."; fi
  if [[ "$free_gb" -ge 30 ]]; then ok "${free_gb} GB free for $data_dir"; else warn "${free_gb} GB free for $data_dir: each world needs about 30 GB."; fi
  if [[ -e /dev/ntsync ]]; then
    ok "/dev/ntsync available (kernel $kernel): worlds can use it for better performance"
  fi
}

# --- configuration --------------------------------------------------------------------

# env_get FILE KEY → value or empty
env_get() {
  if [[ -r "$1" ]]; then
    sed -n "s/^$2=//p" "$1" | tail -n1
  elif [[ -e "$1" ]]; then
    as_root sed -n "s/^$2=//p" "$1" | tail -n1
  fi
}

detect_tz() {
  local tz=""
  if command -v timedatectl >/dev/null 2>&1; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  fi
  if [[ -z "$tz" && -L /etc/localtime ]]; then
    tz="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
  fi
  echo "${tz:-Etc/UTC}"
}

default_image() {
  local version="$1"
  if [[ "$FK_REPO" == __*__ ]]; then
    echo "flamekeeper:dev"
  else
    local owner="${FK_REPO%%/*}"
    # GHCR image names are lowercase.
    echo "ghcr.io/${owner,,}/flamekeeper:${version}"
  fi
}

# Settings precedence: command-line option > existing .env > default.
FK_IMAGE=""
FK_PORT=""
FK_BIND=""
FK_DATA_DIR=""
FK_TZ=""
FK_BASE_PATH=""
resolve_settings() {
  local env_file="$FK_INSTALL_DIR/.env" version
  version="${OPT_VERSION:-}"
  if [[ -z "$version" ]]; then
    version="$FK_RELEASE"
    [[ "$version" == __*__ ]] && version="latest"
  fi
  # A plain re-run upgrades the image to this installer's release.
  FK_IMAGE="${OPT_IMAGE:-$(default_image "$version")}"
  FK_PORT="${OPT_PORT:-$(env_get "$env_file" FLAMEKEEPER_PORT)}"
  FK_PORT="${FK_PORT:-8080}"
  FK_BIND="${OPT_BIND:-$(env_get "$env_file" FLAMEKEEPER_BIND)}"
  FK_BIND="${FK_BIND:-0.0.0.0}"
  FK_DATA_DIR="${OPT_DATA_DIR:-$(env_get "$env_file" DATA_DIR)}"
  FK_DATA_DIR="${FK_DATA_DIR:-/srv/flamekeeper}"
  FK_TZ="$(env_get "$env_file" TZ)"
  FK_TZ="${FK_TZ:-$(detect_tz)}"
  FK_BASE_PATH="$(env_get "$env_file" BASE_PATH)"
}

render_env() {
  cat <<EOF
# FlameKeeper settings. Edit, then run: flamekeeper restart
FLAMEKEEPER_IMAGE=$FK_IMAGE
FLAMEKEEPER_PORT=$FK_PORT
FLAMEKEEPER_BIND=$FK_BIND
DATA_DIR=$FK_DATA_DIR
TZ=$FK_TZ
BASE_PATH=$FK_BASE_PATH
DOCKER_SOCK=$DOCKER_SOCK
EOF
}

script_dir() {
  local src="${BASH_SOURCE[0]:-}"
  [[ -n "$src" && -f "$src" ]] && cd "$(dirname "$src")" && pwd
}

# Embedded content, or the file next to this script when running from a checkout.
embedded_or_file() {
  local content="$1" file="$2" dir
  if [[ "$content" != "__FK_EMBED_"* ]]; then
    printf '%s\n' "$content"
    return
  fi
  dir="$(script_dir || true)"
  [[ -n "$dir" && -f "$dir/$file" ]] || die "Installer is incomplete ($file missing). Download it from the release page."
  cat "$dir/$file"
}

write_files() {
  step "Installing to $FK_INSTALL_DIR"
  local tmp
  tmp="$(mktemp -d)"
  render_env >"$tmp/.env"
  embedded_or_file "$EMBEDDED_COMPOSE" compose.template.yml >"$tmp/docker-compose.yml"
  embedded_or_file "$EMBEDDED_CLI" flamekeeper |
    sed -e "s#__FK_REPO__#$FK_REPO#g" -e "s#__FK_INSTALL_DIR__#$FK_INSTALL_DIR#g" >"$tmp/flamekeeper"

  as_root mkdir -p "$FK_INSTALL_DIR" "$FK_DATA_DIR"
  as_root install -m 0644 "$tmp/.env" "$FK_INSTALL_DIR/.env"
  as_root install -m 0644 "$tmp/docker-compose.yml" "$FK_INSTALL_DIR/docker-compose.yml"
  as_root install -m 0755 "$tmp/flamekeeper" "$FK_BIN_DIR/flamekeeper"
  rm -rf "$tmp"
  ok "$FK_INSTALL_DIR/docker-compose.yml and .env"
  ok "$FK_BIN_DIR/flamekeeper command"
  ok "Data folder $FK_DATA_DIR"
}

# --- firewall ---------------------------------------------------------------------------

configure_firewall() {
  [[ "$OPT_NO_FIREWALL" == 1 ]] && return 0
  local ui="$FK_PORT/tcp" game="$GAME_PORTS/udp"
  if command -v ufw >/dev/null 2>&1 && as_root ufw status 2>/dev/null | grep -q "Status: active"; then
    step "Firewall (ufw)"
    if confirm "Allow the web UI ($ui) and game ports ($game) through ufw?" Y; then
      as_root ufw allow "$ui" >/dev/null && as_root ufw allow "${GAME_PORTS}/udp" >/dev/null
      ok "ufw rules added"
    else
      info "Skipped. To open them later: sudo ufw allow $ui && sudo ufw allow $game"
    fi
  elif command -v firewall-cmd >/dev/null 2>&1 && as_root firewall-cmd --state >/dev/null 2>&1; then
    step "Firewall (firewalld)"
    local fw_game="${GAME_PORTS/:/-}/udp"
    if confirm "Allow the web UI ($ui) and game ports ($fw_game) through firewalld?" Y; then
      as_root firewall-cmd --permanent --add-port="$ui" >/dev/null &&
        as_root firewall-cmd --permanent --add-port="$fw_game" >/dev/null &&
        as_root firewall-cmd --reload >/dev/null
      ok "firewalld rules added"
    else
      info "Skipped. To open them later: sudo firewall-cmd --permanent --add-port=$ui --add-port=$fw_game && sudo firewall-cmd --reload"
    fi
  fi
}

# --- launch ---------------------------------------------------------------------------

primary_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
  fi
  if [[ -z "$ip" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  echo "${ip:-localhost}"
}

# health_setup_error JSON → the setup_error string, or empty when null
health_setup_error() {
  printf '%s' "$1" | sed -n 's/.*"setup_error":"\(\([^"\\]\|\\.\)*\)".*/\1/p'
}

start_controller() {
  step "Starting FlameKeeper"
  cd "$FK_INSTALL_DIR"
  if [[ "$FK_IMAGE" == */* ]]; then
    docker_cmd compose pull --quiet || die "Couldn't pull $FK_IMAGE. Check your internet connection."
  fi
  docker_cmd compose up -d --remove-orphans >/dev/null || die "docker compose up failed. See: flamekeeper logs"
  ok "Container started ($FK_IMAGE)"

  local url="http://127.0.0.1:${FK_PORT}" health="" i
  [[ "$FK_BIND" != "0.0.0.0" ]] && url="http://${FK_BIND}:${FK_PORT}"
  info "Waiting for FlameKeeper to answer…"
  for ((i = 0; i < 60; i++)); do
    if health="$(curl -fsS --max-time 2 "${url}${FK_BASE_PATH%/}/api/health" 2>/dev/null)"; then
      break
    fi
    sleep 2
  done
  [[ -n "$health" ]] || die "FlameKeeper did not start within 2 minutes. See: flamekeeper logs"
  local setup_error
  setup_error="$(health_setup_error "$health")"
  if [[ -n "$setup_error" ]]; then
    die "FlameKeeper started but found a setup problem: $setup_error"
  fi
  ok "FlameKeeper is up"
}

summary() {
  local host
  host="$(primary_ip)"
  [[ "$FK_BIND" != "0.0.0.0" ]] && host="$FK_BIND"
  printf '\n%s%sFlameKeeper is ready.%s\n\n' "$C_BOLD" "$C_GOLD" "$C_RESET"
  printf '    Open  %shttp://%s:%s%s/%s\n' "$C_BOLD" "$host" "$FK_PORT" "${FK_BASE_PATH%/}" "$C_RESET"
  printf '    Data  %s\n' "$FK_DATA_DIR"
  printf '    Manage with: flamekeeper status | logs | update | doctor | uninstall\n\n'
  if [[ "$FK_BIND" == "0.0.0.0" ]]; then
    printf '    %sNote:%s FlameKeeper has no login. Anyone who can reach port %s can control\n' "$C_YEL" "$C_RESET" "$FK_PORT"
    printf '    your servers. Keep it on a trusted network, or re-run with --bind 127.0.0.1\n'
    printf '    and put it behind a reverse proxy with authentication.\n\n'
  fi
  printf '    %sPlayers outside your network need UDP %s forwarded on your router.%s\n\n' "$C_DIM" "$GAME_PORTS" "$C_RESET"
}

main() {
  parse_args "$@"
  printf '%s%sFlameKeeper installer%s %s\n' "$C_BOLD" "$C_GOLD" "$C_RESET" "${FK_RELEASE/__FK_VERSION__/dev}"
  check_platform
  setup_privileges
  ensure_docker
  resolve_settings
  check_resources "$FK_DATA_DIR"
  write_files
  configure_firewall
  start_controller
  summary
}

if [[ "${FK_TESTING:-0}" != 1 ]]; then
  main "$@"
fi
