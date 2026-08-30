import { API_BASE_URL } from "../config/runtime";
import type { ApiErrorShape, JsonRecord } from "../types/api";

export class ApiError extends Error implements ApiErrorShape {
  code: string;
  details: JsonRecord;
  status?: number;

  constructor(shape: ApiErrorShape) {
    super(shape.message);
    this.name = "ApiError";
    this.code = shape.code;
    this.details = shape.details;
    this.status = shape.status;
  }
}

function messageFromDetail(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return undefined;
  return detail
    .map((item) => {
      if (typeof item === "object" && item !== null && "msg" in item) {
        return String(item.msg);
      }
      return String(item);
    })
    .join("; ");
}

async function responseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : undefined;
  } catch {
    payload = undefined;
  }
  if (typeof payload === "object" && payload !== null && "error" in payload) {
    const envelope = (payload as { error?: unknown }).error;
    if (typeof envelope === "object" && envelope !== null) {
      const error = envelope as Record<string, unknown>;
      return new ApiError({
        code: String(error.code || "http_error"),
        message: String(error.message || response.statusText || "请求失败"),
        details:
          typeof error.details === "object" && error.details !== null
            ? (error.details as JsonRecord)
            : {},
        status: response.status,
      });
    }
  }
  const detail =
    typeof payload === "object" && payload !== null && "detail" in payload
      ? (payload as { detail: unknown }).detail
      : undefined;
  return new ApiError({
    code: response.status === 422 ? "validation_error" : "http_error",
    message: messageFromDetail(detail) || text || response.statusText || "请求失败",
    details: {},
    status: response.status,
  });
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (error) {
    throw new ApiError({
      code: "network_error",
      message: error instanceof Error ? error.message : "无法连接 Yuki Gateway",
      details: {},
    });
  }
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as T;
}
