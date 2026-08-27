// Backglass desktop shell.
//
// The app is a WKWebView window over the local FastAPI dashboard. On launch it
// starts `uv run backglass dashboard` in the project directory unless the server
// is already listening, and kills that child when the window closes. There is no
// IPC and no frontend bundle — the dashboard stays server-rendered HTML, per
// docs/10; this shell only gives it a dock icon, a window, and the two macOS
// behaviours a window is expected to have.
//
// What is deliberately NOT here: a menu bar built from scratch. Tauri installs the
// macOS default menu itself when none is set — `Menu::default` carries the app menu,
// File, an Edit submenu with Undo/Redo/Cut/Copy/Paste/Select All, View and Window —
// so the clipboard has always worked and "add a menu" would have been work with no
// output. `Menu::default` is built here only so the Go submenu can be appended to it;
// everything else in that menu is Tauri's, unchanged.
//
// The Go submenu exists because the web layer ran out of keys. base.html maps the ten
// digits to the first ten sidebar entries and there are thirteen pages, so Activity,
// Ask and Scrub had no shortcut and could not be given a letter — letters belong to the
// dashboard (j/k/x/d/s/r, docs/06) and a global letter would fire there too. ⌘-digit is
// free in the webview precisely because the page keys are bare digits, so the shell can
// hand out the accelerators the page cannot.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::menu::{Menu, MenuItem, Submenu};
use tauri::Manager;

const ADDR: &str = "127.0.0.1:8765";

// The window label. tauri.conf.json declares one window and names none, so it is the
// Tauri default; the menu handler needs it to find the webview it should navigate.
const MAIN_WINDOW: &str = "main";

/// The pages the shell puts in the menu, as (menu id, label, accelerator, path).
///
/// The first two are the daily surfaces and would be reachable by a bare digit anyway;
/// they are here because a Go menu that omits the two most-used destinations is a menu
/// about leftovers. The last three are the ones the page keys cannot reach at all.
const GO: &[(&str, &str, &str, &str)] = &[
    ("go-dashboard", "Dashboard", "CmdOrCtrl+1", "/"),
    ("go-schedule", "Schedule", "CmdOrCtrl+2", "/schedule"),
    ("go-activity", "Activity", "CmdOrCtrl+0", "/activity"),
    ("go-ask", "Ask", "CmdOrCtrl+K", "/ask"),
    ("go-scrub", "Scrub", "CmdOrCtrl+J", "/scrub"),
];

fn server_running() -> bool {
    TcpStream::connect_timeout(&ADDR.parse().unwrap(), Duration::from_millis(300)).is_ok()
}

// A Finder-launched app has a minimal PATH, so `uv` must be found explicitly.
#[cfg(debug_assertions)]
fn find_uv() -> PathBuf {
    if let Ok(explicit) = std::env::var("BACKGLASS_UV") {
        return PathBuf::from(explicit);
    }
    if let Ok(home) = std::env::var("HOME") {
        let local = PathBuf::from(home).join(".local/bin/uv");
        if local.exists() {
            return local;
        }
    }
    for candidate in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"] {
        let p = PathBuf::from(candidate);
        if p.exists() {
            return p;
        }
    }
    PathBuf::from("uv")
}

#[cfg(debug_assertions)]
fn project_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("BACKGLASS_DIR") {
        return PathBuf::from(dir);
    }
    // Dev default: this crate lives at <project>/desktop/src-tauri.
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

// Dev builds run the checkout through uv; release builds run the PyInstaller
// sidecar bundled under Resources/, with user data in Application Support so
// the app works with the repo gone entirely.
#[cfg(debug_assertions)]
fn spawn_server() -> Child {
    Command::new(find_uv())
        .args(["run", "backglass", "dashboard"])
        .current_dir(project_dir())
        .spawn()
        .expect("failed to start the backglass dashboard server")
}

#[cfg(not(debug_assertions))]
fn spawn_server() -> Child {
    let exe = std::env::current_exe().expect("no current exe");
    let sidecar = exe
        .parent()
        .and_then(|p| p.parent())
        .map(|contents| {
            contents
                .join("Resources")
                .join("sidecar")
                .join("backglass-server")
                .join("backglass-server")
        })
        .expect("could not resolve the bundled sidecar");
    // cwd is the data home: pydantic-settings reads .env from it and the default
    // db path ./data/backglass.db lands inside it. DB_PATH (absolute) still wins.
    let data_home = std::env::var("HOME")
        .map(PathBuf::from)
        .expect("no HOME")
        .join("Library/Application Support/Backglass");
    std::fs::create_dir_all(&data_home).expect("could not create the data dir");
    Command::new(sidecar)
        .arg("dashboard")
        .current_dir(data_home)
        .spawn()
        .expect("failed to start the bundled backglass server")
}

fn main() {
    let child: Mutex<Option<Child>> = Mutex::new(None);

    if !server_running() {
        *child.lock().unwrap() = Some(spawn_server());
        for _ in 0..150 {
            if server_running() {
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }

    tauri::Builder::default()
        // Window size and position across launches. The shell's own state, kept by the
        // plugin in the app's config dir — nothing about it belongs in the ledger.
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .menu(|handle| {
            // Tauri's default menu, plus one submenu. Building it here rather than
            // letting the builder install it is the only way to append to it, and it is
            // the same menu either way: `Menu::default` is what the builder would have
            // called.
            let menu = Menu::default(handle)?;
            let items = GO
                .iter()
                .map(|(id, label, accel, _)| {
                    MenuItem::with_id(handle, *id, *label, true, Some(*accel))
                })
                .collect::<tauri::Result<Vec<_>>>()?;
            let refs: Vec<&dyn tauri::menu::IsMenuItem<_>> =
                items.iter().map(|i| i as &dyn tauri::menu::IsMenuItem<_>).collect();
            menu.append(&Submenu::with_items(handle, "Go", true, &refs)?)?;
            Ok(menu)
        })
        .on_menu_event(|app, event| {
            let Some(entry) = GO.iter().find(|(id, ..)| *id == event.id().0) else {
                // Not ours — one of Tauri's own predefined items, which handle
                // themselves. Doing nothing here is correct, not a missed case.
                return;
            };
            if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                // A navigation, not an IPC call: the dashboard is server-rendered and
                // the shell has no frontend to talk to. `assign` rather than `replace`
                // so the webview's own history still works.
                let _ = window.eval(format!("window.location.assign({:?})", entry.3));
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(mut c) = child.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        });
}
