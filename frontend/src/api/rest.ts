import type {
  HealthSnapshot,
  LocalModelAcceptedOperation,
  LocalModelOperation,
  LocalModelStatus,
} from "../types/api";
import { requestJson } from "./client";

export function getHealth(signal?: AbortSignal): Promise<HealthSnapshot> {
  return requestJson<HealthSnapshot>("/api/health", { signal });
}

export function getLocalModelStatus(signal?: AbortSignal): Promise<LocalModelStatus> {
  return requestJson<LocalModelStatus>("/api/local-model", { signal });
}

export function setLocalModelEnabled(
  enabled: boolean,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<LocalModelAcceptedOperation> {
  return requestJson<LocalModelAcceptedOperation>("/api/local-model", {
    method: "PUT",
    body: JSON.stringify({ enabled, idempotency_key: idempotencyKey }),
    signal,
  });
}

export function getLocalModelOperation(
  operationId: string,
  signal?: AbortSignal,
): Promise<LocalModelOperation> {
  return requestJson<LocalModelOperation>(
    `/api/local-model/operations/${encodeURIComponent(operationId)}`,
    { signal },
  );
}
