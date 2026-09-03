# Desktop Rust integration notes

## Global voice hotkey verification

Windows verification was performed on 2026-09-02 against the dependency versions locked
in `Cargo.lock`.

1. `tauri-plugin-global-shortcut` 2.3.2 builds and runs with Tauri 2.11.5. The v2 Rust
   integration uses `Builder::new().build()` and `GlobalShortcutExt::on_shortcut`.
2. No `app.plugins` entry is required in `tauri.conf.json` for this Rust-side plugin setup.
3. The web frontend does not call the plugin. Consequently, the existing `core:default`
   capability is sufficient and no global-shortcut command permission is exposed to the
   WebView.
4. Registering `Ctrl+Shift+Space` while another process owns it returns an error without
   preventing the desktop application from starting. The error is reported to the Gateway
   and displayed by the frontend.
5. When another Windows process owns the combination, Windows delivers it to that owner and
   the foreground WebView does not receive a `keydown`. Therefore the same combination cannot
   serve as a window-local fallback in the real conflict case. The product fallback remains
   unresolved pending a choice of an alternate shortcut or button-only operation.

The successful-registration smoke test verified that pressing the combination while Yuki was
unfocused starts listening, pressing it again cancels listening, and foreground use produces
only one toggle. The conflict smoke test used a separate Win32 `RegisterHotKey` owner and
verified the failed-registration status described above.

Run the Rust delivery gate from the repository root with:

```powershell
cargo test --manifest-path frontend/src-tauri/Cargo.toml
cargo check --manifest-path frontend/src-tauri/Cargo.toml
```

## History bootstrap ordering

The chat WebSocket connects before the initial history request so live voice turns remain
observable while history is loading. Only text sending is temporarily gated, with an editable
draft and a loading hint. The gate is released when history settles or after
`CHAT_HISTORY_LOAD_TIMEOUT_MS` (5 seconds); aborted or late responses cannot hydrate the store.
This prevents initial history from overlapping a newly submitted text request, including
requests that fail after the user turn is persisted.

Successful text replies additionally carry persisted user/agent turn IDs through cognition and
the Gateway so message merging uses stable IDs instead of text matching. The frontend has no
toggle/hotkey REST wrappers: those endpoints are consumed directly by the Rust desktop shell.
