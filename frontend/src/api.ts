const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

export const API_BASE = `${basePath}/api`;
export const APP_BASE = basePath;
export const AUTH_REQUIRED_EVENT = "opportunity-radar:auth-required";

export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function appPath(path = ""): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${APP_BASE}${normalized}` || "/";
}

export async function apiJson<T>(
  endpoint: string,
  init: RequestInit = {},
  fallbackMessage = "Opportunity Radar could not complete this request.",
): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${endpoint}`, {
      ...init,
      cache: init.cache ?? (method === "GET" ? "no-store" : undefined),
      credentials: init.credentials ?? "same-origin",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(fallbackMessage);
  }

  if (response.status === 401) window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  const payload = await readJson(response, fallbackMessage);
  if (!response.ok) {
    throw new ApiError(responseMessage(response.status, payload, fallbackMessage), response.status);
  }
  if (payload === undefined) {
    throw new ApiError(`${fallbackMessage} The server returned an empty response.`, response.status);
  }
  return payload as T;
}

export function userMessage(error: unknown, fallbackMessage: string): string {
  return error instanceof ApiError ? error.message : fallbackMessage;
}

async function readJson(response: Response, fallbackMessage: string): Promise<unknown> {
  const text = await response.text();
  if (!text.trim()) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError(`${fallbackMessage} The server returned an invalid response.`, response.status);
  }
}

function responseMessage(status: number, payload: unknown, fallbackMessage: string): string {
  if (status === 401) return "Your Opportunity Radar session has expired. Refresh the page to sign in again.";
  if (status === 403) return "You do not have permission to complete this action.";
  if (status >= 500) return fallbackMessage;
  const detail = extractMessage(payload);
  return detail || fallbackMessage;
}

function extractMessage(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const record = payload as Record<string, unknown>;
  const detail = record.detail;
  const candidate = typeof detail === "string"
    ? detail
    : detail && typeof detail === "object" && typeof (detail as Record<string, unknown>).message === "string"
      ? (detail as Record<string, unknown>).message as string
      : typeof record.message === "string"
        ? record.message
        : "";
  return candidate.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 300);
}
