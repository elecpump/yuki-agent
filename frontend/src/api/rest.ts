import type { HealthSnapshot } from "../types/api";
import { requestJson } from "./client";

export function getHealth(signal?: AbortSignal): Promise<HealthSnapshot> {
  return requestJson<HealthSnapshot>("/api/health", { signal });
}
