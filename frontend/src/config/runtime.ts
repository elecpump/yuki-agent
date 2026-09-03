const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL as string | undefined;

export const API_BASE_URL = (configuredBaseUrl || "http://127.0.0.1:8765").replace(/\/$/, "");
export const STATUS_REST_INTERVAL_MS = 30_000;
export const STATUS_STALE_AFTER_MS = 75_000;
export const LOCAL_MODEL_OPERATION_POLL_MS = 500;
export const VOICE_POLL_MS = 500;
export const CHAT_HISTORY_LOAD_TIMEOUT_MS = 5_000;

export function websocketUrl(path: string): string {
  const url = new URL(path, `${API_BASE_URL}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
