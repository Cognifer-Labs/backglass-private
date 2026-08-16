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

# PyInstaller's googleapiclient hook collects the whole discovery_cache directory
# unless the spec filters it back out. Unfiltered that is 586 files and 99MB of a
# 160MB app, for APIs this program never calls. The spec drops all but the three
# (api, version) pairs __main__._google_service actually builds; this asserts the
# result, because the failure is invisible — the app just quietly triples in size.
DOCS="$ROOT/desktop/sidecar-dist/backglass-server/_internal/googleapiclient/discovery_cache/documents"
FOUND="$(ls "$DOCS" 2>/dev/null | sort | tr '\n' ' ')"
WANT="calendar.v3.json drive.v3.json gmail.v1.json "
if [ "$FOUND" != "$WANT" ]; then
  echo "discovery documents wrong."
  echo "  want: $WANT"
  echo "  got:  $FOUND"
  echo "  (see the filter after Analysis in desktop/sidecar/backglass-server.spec)"
  exit 1
fi
echo "   ok — 3 discovery documents, not 586"

echo "── staging into the Tauri bundle"
rsync -a --delete desktop/sidecar-dist/backglass-server/ desktop/src-tauri/sidecar/backglass-server/
chmod +x desktop/src-tauri/sidecar/backglass-server/backglass-server

# What Python went into this build, so `backglass state` can answer the question it was
# already claiming to answer. It hashes the frozen CSS, templates and scripts and then
# reports `matches_source: True` — about an app whose entire codebase it never looked at,
# because PyInstaller compiles the modules into an archive there is nothing to hash.
# On 2026-08-16 that reported a match against an installed app running the previous
# week's planner. The bundle cannot describe its own Python, so the build records it.
echo "── recording the Python manifest"
MANIFEST="$ROOT/desktop/src-tauri/sidecar/backglass-server/backglass-python.sha256"
( cd "$ROOT" && find backglass -name '*.py' -type f | sort | xargs shasum -a 256 ) > "$MANIFEST"
echo "   $(wc -l < "$MANIFEST" | tr -d ' ') python files recorded"

echo "── tauri build"
cd desktop && npx tauri build

APP="$ROOT/desktop/src-tauri/target/release/bundle/macos/Backglass.app"
# A stable identity, because macOS keys the app's file-access grant (Downloads, where
# the repo and db live) to the code signature. Ad-hoc (-s -) mints a fresh identity
# every build, so every reinstall stalled the sidecar on a consent prompt — a process
# stuck pre-bind at ~0 CPU (tasks/lessons.md 2026-08-06). Preference order: an explicit
# BACKGLASS_SIGN_IDENTITY, else the first Apple Development identity in the keychain,
# else ad-hoc so a clean clone still builds.
IDENTITY="${BACKGLASS_SIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null \
  | grep -o '"Apple Development: [^"]*"' | head -1 | tr -d '"')}"
if [ -n "$IDENTITY" ]; then
  echo "── signing as: $IDENTITY"
  codesign --force --deep -s "$IDENTITY" "$APP"
else
  echo "── ad-hoc signing (no stable identity found; expect a permission prompt per rebuild)"
  codesign --force --deep -s - "$APP"
fi
echo "built: $APP"
du -sh "$APP"
