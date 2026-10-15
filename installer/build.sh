#!/usr/bin/env bash
# Build the release installer: embed the CLI and compose template into install.sh and
# set the repository and version. Output: dist/install.sh and dist/install.sh.sha256.
#
#   installer/build.sh <owner/repo> <version>
set -euo pipefail

repo="${1:?usage: build.sh <owner/repo> <version>}"
version="${2:?usage: build.sh <owner/repo> <version>}"
version="${version#v}"
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "bad repo: $repo" >&2; exit 1; }
[[ "$version" =~ ^[0-9A-Za-z.+-]+$ ]] || { echo "bad version: $version" >&2; exit 1; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${OUT_DIR:-$here/../dist}"
mkdir -p "$out"

# The embedded files must not contain the heredoc terminators.
for f in flamekeeper compose.template.yml; do
  if grep -qE '^(FK_EMBED_CLI|FK_EMBED_COMPOSE)$' "$here/$f"; then
    echo "$f contains a heredoc terminator line" >&2
    exit 1
  fi
done

awk -v cli="$here/flamekeeper" -v compose="$here/compose.template.yml" '
  $0 == "__FK_EMBED_CLI__"     { while ((getline line < cli) > 0) print line; next }
  $0 == "__FK_EMBED_COMPOSE__" { while ((getline line < compose) > 0) print line; next }
  { print }
' "$here/install.sh" |
  sed -e "s#^FK_REPO=\"__FK_REPO__\"#FK_REPO=\"$repo\"#" \
    -e "s#^FK_RELEASE=\"__FK_VERSION__\"#FK_RELEASE=\"$version\"#" \
    -e "s#github.com/__FK_REPO__/#github.com/$repo/#g" >"$out/install.sh"
chmod 0755 "$out/install.sh"

(cd "$out" && sha256sum install.sh >install.sh.sha256)
echo "built $out/install.sh ($repo $version)"
