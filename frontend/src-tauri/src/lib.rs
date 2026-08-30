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

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::System::Console::{
    AttachConsole, FreeConsole, GenerateConsoleCtrlEvent, SetConsoleCtrlHandler, CTRL_BREAK_EVENT,
};

const GATEWAY_ADDRESS: &str = "127.0.0.1:8765";
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

fn probe_gateway() -> ProbeResult {
    let address: SocketAddr = GATEWAY_ADDRESS
        .parse()
        .expect("static gateway address is valid");
    let mut stream = match TcpStream::connect_timeout(&address, Duration::from_secs(1)) {
        Ok(stream) => stream,
        Err(_) => return ProbeResult::Unreachable,
    };
    let timeout = Some(Duration::from_secs(1));
    let _ = stream.set_read_timeout(timeout);
    let _ = stream.set_write_timeout(timeout);
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
        .manage(BackendState::default())
        .setup(|app| {
            configure_backend(app.state::<BackendState>().inner());
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
