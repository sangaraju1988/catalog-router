#!/usr/bin/env sh
# catalog-router installer
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/catalog-router/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --configure-claude
#
# What it does:
#   1. Checks Docker is installed and running.
#   2. Pulls the image (or builds it, when run from a source checkout).
#   3. Creates ~/.catalog-router/catalog.yaml from the bundled example (never overwrites).
#   4. Prints the Claude Desktop MCP config, or merges it in with --configure-claude
#      (a timestamped backup of your config is written first).
#
# Env overrides: CATALOG_ROUTER_IMAGE, CATALOG_ROUTER_HOME

set -eu

IMAGE="${CATALOG_ROUTER_IMAGE:-ghcr.io/sangaraju1988/catalog-router:0.1.2}"
HOME_DIR="${CATALOG_ROUTER_HOME:-$HOME/.catalog-router}"
CATALOG="$HOME_DIR/catalog.yaml"
CONFIGURE_CLAUDE=0

for arg in "$@"; do
  case "$arg" in
    --configure-claude) CONFIGURE_CLAUDE=1 ;;
    -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Get Docker Desktop: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start Docker Desktop and re-run."

say "Getting image $IMAGE"
LOCAL_IMAGE="catalog-router:0.1.2"
if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
  if docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
    IMAGE="$LOCAL_IMAGE"
    say "Pull failed; using existing local image $IMAGE"
  elif [ -f Dockerfile ] && [ -f pyproject.toml ] && grep -q 'name = "catalog-router"' pyproject.toml; then
    IMAGE="$LOCAL_IMAGE"
    say "Pull failed; building $IMAGE from this source checkout"
    docker build -q -t "$IMAGE" . >/dev/null
  else
    fail "could not pull $IMAGE. Set CATALOG_ROUTER_IMAGE, or run this script from a clone of the repo to build locally."
  fi
fi

mkdir -p "$HOME_DIR"
if [ -f "$CATALOG" ]; then
  say "Keeping existing catalog at $CATALOG"
else
  docker run --rm --entrypoint cat "$IMAGE" /catalog/catalog.yaml > "$CATALOG"
  say "Wrote example catalog to $CATALOG (edit it to describe your BI stack)"
fi

docker run --rm --network none -v "$CATALOG:/catalog/catalog.yaml:ro" "$IMAGE" --check \
  || fail "catalog at $CATALOG is invalid (see message above)"

# Hardened stdio launch: no network, read-only FS, no capabilities, non-root.
SERVER_JSON=$(cat <<EOF
{
  "command": "docker",
  "args": ["run", "-i", "--rm", "--network", "none", "--read-only",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "-v", "$CATALOG:/catalog/catalog.yaml:ro", "$IMAGE"]
}
EOF
)

case "$(uname -s)" in
  Darwin) CLAUDE_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
  Linux)  CLAUDE_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/Claude/claude_desktop_config.json" ;;
  *)      CLAUDE_CFG="" ;;
esac

if [ "$CONFIGURE_CLAUDE" -eq 1 ]; then
  [ -n "$CLAUDE_CFG" ] || fail "--configure-claude supports macOS and Linux; add the JSON below manually."
  command -v python3 >/dev/null 2>&1 || fail "--configure-claude needs python3 to edit JSON safely."
  mkdir -p "$(dirname "$CLAUDE_CFG")"
  if [ -f "$CLAUDE_CFG" ]; then
    cp "$CLAUDE_CFG" "$CLAUDE_CFG.bak.$(date +%Y%m%d%H%M%S)"
  fi
  SERVER_JSON="$SERVER_JSON" CLAUDE_CFG="$CLAUDE_CFG" python3 - <<'PY'
import json, os
path = os.environ["CLAUDE_CFG"]
cfg = {}
if os.path.exists(path) and os.path.getsize(path) > 0:
    with open(path) as f:
        cfg = json.load(f)
cfg.setdefault("mcpServers", {})["catalog-router"] = json.loads(os.environ["SERVER_JSON"])
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
  say "Added 'catalog-router' to $CLAUDE_CFG (backup saved). Restart Claude Desktop."
else
  say "Add this under \"mcpServers\" in your MCP client config${CLAUDE_CFG:+ ($CLAUDE_CFG)}:"
  printf '\n"catalog-router": %s\n\n' "$SERVER_JSON"
  say "Or re-run with --configure-claude to do it automatically."
fi

say "Done. Try asking: \"Which system should answer: how is net revenue calculated?\""
