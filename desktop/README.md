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

## What the shell adds

Two macOS behaviours, and deliberately no more — the dashboard stays server-rendered HTML
(docs/10) and there is still no IPC and no frontend bundle.

- **Window size and position** persist across launches (`tauri-plugin-window-state`).
- **A Go menu**: Dashboard ⌘1, Schedule ⌘2, Activity ⌘0, Ask ⌘K, Scrub ⌘J. Each item is a
  navigation in the webview, not an IPC call.

  It exists because the web layer ran out of keys. `base.html` maps the ten digits to the
  first ten sidebar entries and there are thirteen pages, so Activity, Ask and Scrub have
  no shortcut in the page — and cannot be given a letter, because letters belong to the
  dashboard (`j`/`k`/`x`/`d`/`s`/`r`). ⌘-digit is free in the webview precisely because
  the page keys are bare digits, so the shell hands out the accelerators the page cannot.

**The rest of the menu bar is Tauri's, not ours.** Tauri v2 installs the macOS default
menu when the builder sets none — app menu, File, Edit with Undo/Redo/Cut/Copy/Paste/Select
All, View, Window — so the clipboard has always worked here. `Menu::default` is built
explicitly in `main.rs` only so the Go submenu can be appended to it; nothing else in that
menu is written by this crate. A plan to "add a menu bar so ⌘C works" was written and
then deleted on 2026-08-25 after reading `tauri-2.11.5/src/app.rs:2244`; do not re-add it.

## Configuration

The shell resolves two things at launch, both overridable:

- `BACKGLASS_DIR` — project directory to run the server in (default: the repo this
  crate sits in, compiled in via `CARGO_MANIFEST_DIR`; set this if you move the app).
- `BACKGLASS_UV` — path to `uv` (default: `~/.local/bin/uv`, then Homebrew paths).
  Needed because Finder-launched apps get a minimal `PATH`.

Server config itself comes from the project's `.env`, as everywhere else.
