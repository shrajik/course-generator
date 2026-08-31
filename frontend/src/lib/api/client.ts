/**
 * The single place that knows how to talk to the FastAPI backend.
 * UI components never call fetch directly - they go through lib/api/*.
 */

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    code = "error",
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

interface BackendErrorBody {
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
  detail?: unknown;
}

function readableDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          const record = item as { loc?: unknown[]; msg?: string };
          const field = Array.isArray(record.loc) ? record.loc.slice(1).join(".") : "";
          return field ? `${field}: ${record.msg}` : String(record.msg);
        }
        return JSON.stringify(item);
      })
      .join("; ");
  }
  return "Request failed";
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: BackendErrorBody = {};
  try {
    body = (await response.json()) as BackendErrorBody;
  } catch {
    return new ApiError(`${response.status} ${response.statusText}`, response.status);
  }
  if (body.error) {
    return new ApiError(
      body.error.message ?? "Request failed",
      response.status,
      body.error.code ?? "error",
      body.error.details ?? {},
    );
  }
  return new ApiError(readableDetail(body.detail), response.status, "validation_failed");
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | undefined>;
}

export function apiUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(`${API_BASE_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, query } = options;

  let response: Response;
  try {
    response = await fetch(apiUrl(path, query), {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE_URL}. Is it running?`,
      0,
      "network_error",
    );
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function requestBlob(
  path: string,
  options: RequestOptions = {},
): Promise<Blob> {
  const { method = "GET", body, signal, query } = options;
  let response: Response;
  try {
    response = await fetch(apiUrl(path, query), {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(`Cannot reach the backend at ${API_BASE_URL}.`, 0, "network_error");
  }
  if (!response.ok) throw await toApiError(response);
  return response.blob();
}
