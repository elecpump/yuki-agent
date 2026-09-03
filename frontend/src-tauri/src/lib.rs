use serde_json::Value;
use std::env;
use std::ffi::OsString;
use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::System::Console::{
    AttachConsole, FreeConsole, GenerateConsoleCtrlEvent, SetConsoleCtrlHandler, CTRL_BREAK_EVENT,
};

const GATEWAY_ADDRESS: &str = "127.0.0.1:8765";
const VOICE_HOTKEY: &str = "Ctrl+Shift+Space";
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const CTRL_BREAK_GRACE_PERIOD: Duration = Duration::from_secs(2);
const FORCE_KILL_GRACE_PERIOD: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbeResult {
    Yuki,
    Unreachable,
    Occupied,
}

#[derive(Default)]
struct BackendState {
    owned: Mutex<Option<Child>>,
}

fn connect_gateway(address: &SocketAddr) -> std::io::Result<TcpStream> {
    let stream = TcpStream::connect_timeout(address, Duration::from_secs(1))?;
    let timeout = Some(Duration::from_secs(1));
    stream.set_read_timeout(timeout)?;
    stream.set_write_timeout(timeout)?;
    Ok(stream)
}

fn probe_gateway() -> ProbeResult {
    let address: SocketAddr = GATEWAY_ADDRESS
        .parse()
        .expect("static gateway address is valid");
    let mut stream = match connect_gateway(&address) {
        Ok(stream) => stream,
        Err(_) => return ProbeResult::Unreachable,
    };
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return ProbeResult::Occupied;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return ProbeResult::Occupied;
    }
    let Some((headers, body)) = response.split_once("\r\n\r\n") else {
        return ProbeResult::Occupied;
    };
    if !headers
        .lines()
        .next()
        .is_some_and(|line| line.contains(" 200 "))
    {
        return ProbeResult::Occupied;
    }
    let Ok(payload) = serde_json::from_str::<Value>(body) else {
        return ProbeResult::Occupied;
    };
    if payload.get("gateway").is_some()
        && payload.get("hub").is_some()
        && payload.get("processes").is_some()
    {
        ProbeResult::Yuki
    } else {
        ProbeResult::Occupied
    }
}

fn build_gateway_post_request(path: &str, body: &str) -> String {
    format!(
        "POST {path} HTTP/1.1\r\n\
         Host: 127.0.0.1:8765\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n\
         {body}",
        body.len(),
    )
}

fn post_gateway_json_to(address: SocketAddr, path: &str, body: &str) -> bool {
    let mut stream = match connect_gateway(&address) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    if stream
        .write_all(build_gateway_post_request(path, body).as_bytes())
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok()
        && response
            .lines()
            .next()
            .is_some_and(|line| line.contains(" 200 "))
}

fn post_gateway_json(path: &str, body: &str) -> bool {
    let address = GATEWAY_ADDRESS
        .parse()
        .expect("static gateway address is valid");
    post_gateway_json_to(address, path, body)
}

fn report_hotkey_status(registered: bool, error: Option<String>) {
    thread::spawn(move || {
        let body = serde_json::json!({
            "registered": registered,
            "error": error.unwrap_or_default(),
        })
        .to_string();
        for _ in 0..40 {
            if post_gateway_json("/api/voice/hotkey", &body) {
                return;
            }
            thread::sleep(Duration::from_millis(250));
        }
    });
}

fn configure_global_hotkey(app: &tauri::App) {
    let result = app
        .global_shortcut()
        .on_shortcut(VOICE_HOTKEY, |_app, _shortcut, event| {
            if event.state() == ShortcutState::Pressed {
                thread::spawn(|| {
                    let _ = post_gateway_json("/api/voice/toggle", "{}");
                });
            }
        });
    match result {
        Ok(()) => report_hotkey_status(true, None),
        Err(error) => {
            eprintln!("[yuki-desktop] global voice hotkey unavailable: {error}");
            report_hotkey_status(false, Some(error.to_string()));
        }
    }
}

fn env_flag(name: &str) -> Option<bool> {
    env::var(name)
        .ok()
        .and_then(|value| match value.to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" => Some(true),
            "0" | "false" | "no" => Some(false),
            _ => None,
        })
}

fn python_invocation() -> Result<(OsString, Vec<OsString>), String> {
    if let Some(path) = env::var_os("YUKI_PYTHON") {
        if !Path::new(&path).is_absolute() {
            return Err("YUKI_PYTHON must be an absolute path".into());
        }
        return Ok((path, Vec::new()));
    }
    Ok((OsString::from("py"), vec![OsString::from("-3")]))
}

fn workdir() -> Result<PathBuf, String> {
    let value = env::var_os("YUKI_WORKDIR").ok_or("YUKI_WORKDIR is required")?;
    let path = PathBuf::from(value);
    if !path.is_absolute() || !path.is_dir() {
        return Err("YUKI_WORKDIR must be an existing absolute directory".into());
    }
    if !path.join("config.yaml").is_file() {
        return Err("YUKI_WORKDIR must contain config.yaml".into());
    }
    fs::canonicalize(path).map_err(|error| format!("cannot resolve YUKI_WORKDIR: {error}"))
}

fn command_with_base(program: &OsString, base_args: &[OsString]) -> Command {
    let mut command = Command::new(program);
    command.args(base_args);
    command
}

fn start_backend() -> Result<Child, String> {
    let directory = workdir()?;
    let (program, base_args) = python_invocation()?;

    let mut preflight = command_with_base(&program, &base_args);
    let status = preflight
        .current_dir(&directory)
        .args(["-c", "import yuki"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("cannot run Yuki Python preflight: {error}"))?;
    if !status.success() {
        return Err("the configured Python interpreter cannot import yuki".into());
    }

    let mut supervisor = command_with_base(&program, &base_args);
    supervisor
        .current_dir(directory)
        .args(["-m", "yuki.supervisor"])
        .env("YUKI_GATEWAY_ENABLED", "true")
        .stdin(Stdio::null());
    #[cfg(windows)]
    supervisor.creation_flags(CREATE_NEW_PROCESS_GROUP);
    supervisor
        .spawn()
        .map_err(|error| format!("cannot start yuki supervisor: {error}"))
}

fn configure_backend(state: &BackendState) {
    // v1 diagnostics intentionally use stderr. A release GUI process normally has no visible
    // console, so these messages are only observable from terminal-launched debug builds.
    // Replace this with a persistent logger (for example tauri-plugin-log) in the v2 bundle.
    match probe_gateway() {
        ProbeResult::Yuki => {
            eprintln!("[yuki-desktop] using external Yuki Gateway");
        }
        ProbeResult::Occupied => {
            eprintln!("[yuki-desktop] port 8765 is occupied by a non-Yuki service");
        }
        ProbeResult::Unreachable => {
            let default_launch = !cfg!(debug_assertions);
            if !env_flag("YUKI_DESKTOP_LAUNCH_BACKEND").unwrap_or(default_launch) {
                eprintln!("[yuki-desktop] backend auto-launch is disabled");
                return;
            }
            match start_backend() {
                Ok(child) => {
                    eprintln!("[yuki-desktop] started owned supervisor pid={}", child.id());
                    *state.owned.lock().expect("backend state poisoned") = Some(child);
                }
                Err(error) => eprintln!("[yuki-desktop] {error}"),
            }
        }
    }
}

#[cfg(windows)]
fn try_send_ctrl_break(process_group_id: u32) -> bool {
    unsafe {
        // A GUI process normally has no console. Attach to the child's console for the spike path.
        let _ = FreeConsole();
        if AttachConsole(process_group_id) == 0 {
            return false;
        }
        let _ = SetConsoleCtrlHandler(None, 1);
        let sent = GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, process_group_id) != 0;
        let _ = FreeConsole();
        let _ = SetConsoleCtrlHandler(None, 0);
        sent
    }
}

#[cfg(not(windows))]
fn try_send_ctrl_break(_process_group_id: u32) -> bool {
    false
}

fn wait_for_exit(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => thread::sleep(Duration::from_millis(100)),
            Err(_) => return false,
        }
    }
    false
}

fn force_kill_tree(pid: u32) -> bool {
    #[cfg(windows)]
    {
        let pid_str = pid.to_string();
        Command::new("taskkill")
            .args(["/PID", pid_str.as_str(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
        false
    }
}

fn shutdown_backend(state: &BackendState) {
    let Some(mut child) = state.owned.lock().expect("backend state poisoned").take() else {
        return;
    };
    let pid = child.id();
    if try_send_ctrl_break(pid) {
        if wait_for_exit(&mut child, CTRL_BREAK_GRACE_PERIOD) {
            eprintln!("[yuki-desktop] supervisor exited after CTRL_BREAK");
            return;
        }
        eprintln!("[yuki-desktop] CTRL_BREAK timed out; forcing process tree shutdown");
    } else {
        eprintln!("[yuki-desktop] CTRL_BREAK delivery failed; forcing process tree shutdown");
    }

    if force_kill_tree(pid) && wait_for_exit(&mut child, FORCE_KILL_GRACE_PERIOD) {
        eprintln!("[yuki-desktop] supervisor process tree terminated by taskkill");
        return;
    }

    // Never block application exit indefinitely if taskkill is unavailable or reports success
    // before the root process actually terminates. Child::kill is a last-resort root-only fallback.
    eprintln!("[yuki-desktop] taskkill failed or timed out; killing supervisor root process");
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn builds_gateway_json_post_request() {
        let request = build_gateway_post_request("/api/voice/hotkey", r#"{"registered":true}"#);

        assert!(request.starts_with("POST /api/voice/hotkey HTTP/1.1\r\n"));
        assert!(request.contains("Content-Type: application/json\r\n"));
        assert!(request.contains("Content-Length: 19\r\n"));
        assert!(request.ends_with("\r\n\r\n{\"registered\":true}"));
    }

    #[test]
    fn posts_json_to_gateway_address() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let address = listener.local_addr().expect("read listener address");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept request");
            let mut request = [0_u8; 512];
            let read = stream.read(&mut request).expect("read request");
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                .expect("write response");
            String::from_utf8_lossy(&request[..read]).into_owned()
        });

        assert!(post_gateway_json_to(address, "/api/voice/toggle", "{}",));
        assert!(server
            .join()
            .expect("join server")
            .starts_with("POST /api/voice/toggle HTTP/1.1\r\n",));
    }

    #[test]
    fn wait_for_exit_detects_completed_child() {
        let mut child = Command::new("cmd.exe")
            .args(["/d", "/c", "exit", "0"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn completed child");

        assert!(wait_for_exit(&mut child, Duration::from_secs(2)));
    }

    #[test]
    fn taskkill_fallback_terminates_owned_process() {
        let mut child = Command::new("cmd.exe")
            .args(["/d", "/c", "ping", "-t", "127.0.0.1"])
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn isolated process group");
        let pid = child.id();

        assert!(force_kill_tree(pid));
        assert!(wait_for_exit(&mut child, Duration::from_secs(3)));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(BackendState::default())
        .setup(|app| {
            configure_backend(app.state::<BackendState>().inner());
            configure_global_hotkey(app);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Yuki desktop application");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            shutdown_backend(app_handle.state::<BackendState>().inner());
        }
    });
}
