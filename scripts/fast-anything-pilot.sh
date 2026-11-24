#!/usr/bin/env bash
set -euo pipefail

readonly PACKAGE='@el4cteo/fast-anything'
readonly DEFAULT_VERSION='0.8.6'
readonly INSTALL_DIR="${HOME}/.local/share/projectflow-tools/fast-anything"
readonly MARKER='.projectflow-fast-anything-install'

usage() {
  cat <<'USAGE'
Usage:
  scripts/fast-anything-pilot.sh install [VERSION]
  scripts/fast-anything-pilot.sh check [VERSION]
  scripts/fast-anything-pilot.sh uninstall

Installs the exact npm version into ~/.local/share/projectflow-tools/fast-anything.
No global npm state, PATH, or system files are modified.
USAGE
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

validate_version() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "version must be an exact stable x.y.z value: $1"
}

check_node() {
  command -v node >/dev/null || fail 'Node.js is required.'
  command -v npm >/dev/null || fail 'npm is required.'
  node -e 'const [a]=process.versions.node.split(".").map(Number); if(a<18) process.exit(1)' \
    || fail "Node.js >=18 is required; found $(node --version)."
}

check_owned_directory() {
  [[ ! -L "$INSTALL_DIR" ]] || fail "refusing symlink install path: $INSTALL_DIR"
  [[ -d "$INSTALL_DIR" ]] || fail "install directory is absent: $INSTALL_DIR"
  [[ -f "$INSTALL_DIR/$MARKER" ]] || fail "install directory is not marked as pilot-owned: $INSTALL_DIR"
  [[ "$(cat "$INSTALL_DIR/$MARKER")" == 'projectflow fast-anything pilot install' ]] \
    || fail "ownership marker did not match; refusing to alter $INSTALL_DIR"
}

install_tool() {
  local version="$1" created=0
  validate_version "$version"
  check_node

  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [[ -e "$INSTALL_DIR" || -L "$INSTALL_DIR" ]]; then
    check_owned_directory
  else
    mkdir -m 700 "$INSTALL_DIR"
    created=1
  fi

  if ! npm install --prefix "$INSTALL_DIR" --save-exact --no-audit --no-fund "$PACKAGE@$version"; then
    if (( created )); then rm -rf -- "$INSTALL_DIR"; fi
    fail 'npm installation failed; no pre-existing installation was removed.'
  fi

  printf '%s\n' 'projectflow fast-anything pilot install' > "$INSTALL_DIR/$MARKER"
  check_tool "$version"
}

check_tool() {
  local expected="$1" bin="$INSTALL_DIR/node_modules/.bin/fast-anything" actual
  validate_version "$expected"
  check_owned_directory
  [[ -x "$bin" ]] || fail "fast-anything executable missing: $bin"
  actual="$(node -e 'const fs=require("node:fs");process.stdout.write(JSON.parse(fs.readFileSync(process.argv[1],"utf8")).version)' "$INSTALL_DIR/node_modules/$PACKAGE/package.json")"
  [[ "$actual" == "$expected" ]] || fail "installed version $actual differs from requested $expected"
  printf 'package version: %s\n' "$actual"
  printf 'binary: %s\n' "$bin"
  "$bin" --version
}

uninstall_tool() {
  if [[ ! -e "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]]; then
    printf 'Pilot install is already absent: %s\n' "$INSTALL_DIR"
    return 0
  fi
  check_owned_directory
  rm -rf -- "$INSTALL_DIR"
  printf 'Removed pilot-owned install directory: %s\n' "$INSTALL_DIR"
}

command_name="${1:-}"
case "$command_name" in
  install)
    install_tool "${2:-$DEFAULT_VERSION}"
    ;;
  check)
    check_tool "${2:-$DEFAULT_VERSION}"
    ;;
  uninstall)
    [[ $# -eq 1 ]] || fail 'uninstall does not accept a version.'
    uninstall_tool
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
