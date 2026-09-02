export type JsonRecord = Record<string, unknown>;

export interface ComponentHealth {
  ok: boolean;
  detail: JsonRecord;
}

export interface ProcessHealth {
  process: string;
  ts?: number;
  healthy: boolean;
  fresh: boolean;
  last_seen_age_s: number | null;
  components: Record<string, ComponentHealth>;
  pid?: number;
  uptime_s?: number;
  error_count?: number;
}

export interface GatewayHealth {
  healthy: boolean;
  process: string;
  started: boolean;
  ts: number;
}

export interface HubHealth extends JsonRecord {
  healthy: boolean | null;
  cached?: boolean;
  error?: string;
  process?: string;
  pid?: number;
  uptime_s?: number;
  error_count?: number;
  components?: Record<string, JsonRecord>;
}

export interface HealthSnapshot {
  gateway: GatewayHealth;
  hub: HubHealth;
  processes: Record<string, ProcessHealth>;
}

export interface ApiErrorShape {
  code: string;
  message: string;
  details: JsonRecord;
  status?: number;
}

export type VoiceState = "idle" | "listening" | "speaking" | "processing" | "tts";

export interface VoiceStatus {
  available: boolean;
  state: VoiceState;
  session_id: number | null;
  active: boolean;
}

export type LocalModelState =
  | "unavailable"
  | "disabled"
  | "enabling"
  | "enabled"
  | "disabling"
  | "recovering"
  | "failed";

export interface LocalModelStatus {
  available: boolean;
  enabled: boolean;
  target_enabled: boolean;
  state: LocalModelState;
  runtime_state: string;
  loaded: boolean;
  active_calls: number;
  operation: LocalModelOperation | null;
  last_error: string;
}

export interface LocalModelAcceptedOperation {
  operation_id: string;
  accepted: boolean;
  target_enabled: boolean;
}

export type LocalModelOperationState =
  | "queued"
  | "running"
  | "recovering"
  | "succeeded"
  | "failed";

export interface LocalModelOperation {
  operation_id: string;
  target_enabled: boolean;
  state: LocalModelOperationState;
  error_code: string | null;
}
