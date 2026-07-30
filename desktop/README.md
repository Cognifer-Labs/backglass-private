# Backglass desktop shell

A Tauri v2 window over the local dashboard. No frontend bundle, no IPC — the app
starts `uv run backglass dashboard` (unless something already listens on
`127.0.0.1:8765`), waits for the port, and opens a WKWebView on it. Closing the
window kills the server it started; a server it found already running is left alone.

## Build

```sh
cd desktop
npm install
npx tauri build        # → src-tauri/target/release/bundle/macos/Backglass.app
```

`npx tauri dev` for a debug run.

## Configuration

The shell resolves two things at launch, both overridable:

- `BACKGLASS_DIR` — project directory to run the server in (default: the repo this
  crate sits in, compiled in via `CARGO_MANIFEST_DIR`; set this if you move the app).
- `BACKGLASS_UV` — path to `uv` (default: `~/.local/bin/uv`, then Homebrew paths).
  Needed because Finder-launched apps get a minimal `PATH`.

Server config itself comes from the project's `.env`, as everywhere else.
