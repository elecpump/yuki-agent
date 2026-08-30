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
