#!/bin/bash
# Build the self-contained Backglass.app: PyInstaller sidecar + Tauri shell.
# Run from anywhere; operates on the repo this script lives in.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "── freezing the backend (PyInstaller onedir)"
uv sync -q
uv run --with pyinstaller pyinstaller desktop/sidecar/backglass-server.spec \
  --noconfirm --distpath desktop/sidecar-dist --workpath desktop/sidecar-build

BIN="$ROOT/desktop/sidecar-dist/backglass-server/backglass-server"

echo "── smoke test from outside the repo"
SMOKE_DIR="$(mktemp -d)"
pushd "$SMOKE_DIR" >/dev/null
"$BIN" --help >/dev/null
DB_PATH="$SMOKE_DIR/t.db" "$BIN" dashboard --port 8991 &
SIDECAR_PID=$!
trap 'kill $SIDECAR_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf -o /dev/null http://127.0.0.1:8991/ && break
  sleep 0.2
done
curl -sf -o /dev/null http://127.0.0.1:8991/ || { echo "dashboard did not serve"; exit 1; }
curl -sf -o /dev/null http://127.0.0.1:8991/design/tokens.css || { echo "frozen resources missing"; exit 1; }
kill $SIDECAR_PID; trap - EXIT
popd >/dev/null
echo "   ok — served / and /design/tokens.css from a frozen tree"

echo "── staging into the Tauri bundle"
rsync -a --delete desktop/sidecar-dist/backglass-server/ desktop/src-tauri/sidecar/backglass-server/
chmod +x desktop/src-tauri/sidecar/backglass-server/backglass-server

echo "── tauri build"
cd desktop && npx tauri build

APP="$ROOT/desktop/src-tauri/target/release/bundle/macos/Backglass.app"
echo "── ad-hoc signing"
codesign --force --deep -s - "$APP"
echo "built: $APP"
du -sh "$APP"
