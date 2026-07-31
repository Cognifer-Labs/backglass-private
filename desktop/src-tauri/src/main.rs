// Backglass desktop shell.
//
// The app is a WKWebView window over the local FastAPI dashboard. On launch it
// starts `uv run backglass dashboard` in the project directory unless the server
// is already listening, and kills that child when the window closes. There is no
// IPC and no frontend bundle — the dashboard stays server-rendered HTML, per
// docs/10; this shell only gives it a dock icon and a window.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

const ADDR: &str = "127.0.0.1:8765";

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
