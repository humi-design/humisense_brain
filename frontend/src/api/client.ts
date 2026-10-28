/**
 * Typed API client.
 *
 * Security properties, by design:
 * - Authentication is a first-party HttpOnly session cookie. No token is ever read,
 *   stored, or sent by this code, so there is nothing in local/session storage for an
 *   XSS payload to steal.
 * - State-changing requests echo the CSRF token from its (readable) cookie in a header,
 *   which the backend compares against the session-bound hash.
 * - Error bodies are parsed defensively: a proxy or upstream returning HTML must not
 *   crash the UI or leak markup into the DOM.
 * - Responses are validated with the zod schemas in `schemas.ts` before use.
 */

import { z } from "zod";
import type {
  AuditEventList,
  Connection,
  ConnectionList,
  ConnectionCreateRequest,
  ConnectionStatus,
  ConnectionTestResult,
  Connector,
  CurrentUser,
  Overview,
  SourceRecordList,
  SyncAccepted,
  SyncJob,
  SyncJobList,
} from "./types";
import {
  auditEventListSchema,
  connectionListSchema,
  connectionSchema,
  connectionStatusSchema,
  connectionTestResultSchema,
  connectorListSchema,
  currentUserSchema,
  overviewSchema,
  sourceRecordListSchema,
  syncAcceptedSchema,
  syncJobListSchema,
  syncJobSchema,
} from "./schemas";

const API_PREFIX = "/api/v1";
const CSRF_COOKIE = "humisense_csrf";
const CSRF_HEADER = "X-CSRF-Token";

/** An error carrying the backend's machine-readable code, never a raw stack trace. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly details: Record<string, unknown> | null;

  constructor(
    status: number,
    code: string,
    message: string,
    correlationId: string | null,
    details: Record<string, unknown> | null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.details = details;
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) {
      return decodeURIComponent(part.slice(prefix.length));
    }
  }
  return null;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = `${API_PREFIX}${path}`;
  if (!query) {
    return url;
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      params.set(key, String(value));
    }
  }
  const serialised = params.toString();
  return serialised ? `${url}?${serialised}` : url;
}

async function request<T>(
  path: string,
  schema: z.ZodType<T>,
  options: RequestOptions = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const headers = new Headers({ Accept: "application/json" });

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET" && method !== "HEAD") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) {
      headers.set(CSRF_HEADER, csrf);
    }
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      // Send the session cookie, and never attach credentials cross-origin.
      credentials: "same-origin",
      ...(options.body !== undefined
        ? { body: JSON.stringify(options.body) }
        : {}),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw cause;
    }
    throw new ApiError(
      0,
      "network_error",
      "The backend could not be reached. Check that it is running.",
      null,
      null,
    );
  }

  if (response.status === 204) {
    return schema.parse(undefined);
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // A non-JSON body (proxy error page, gateway HTML) is not surfaced to the user.
      parsed = null;
    }
  }

  if (!response.ok) {
    const envelope = parsed as
      | { error?: { code?: string; message?: string; correlation_id?: string; details?: Record<string, unknown> } }
      | null;
    throw new ApiError(
      response.status,
      envelope?.error?.code ?? `http_${response.status}`,
      envelope?.error?.message ?? "The request could not be completed.",
      envelope?.error?.correlation_id ?? null,
      envelope?.error?.details ?? null,
    );
  }

  const result = schema.safeParse(parsed);
  if (!result.success) {
    // A contract mismatch is a real defect, but it must not be reported as a stack trace.
    throw new ApiError(
      response.status,
      "response_validation_failed",
      "The server returned an unexpected response shape.",
      null,
      null,
    );
  }
  return result.data;
}

/** Development sign-in. Refused by the backend whenever AUTH_MODE is not `dev`. */
export interface DevSignInRequest {
  subject: string;
  organization_slug: string;
  email?: string | null;
  display_name?: string | null;
}

/** Production sign-in: exchange a provider-issued id_token for a session. */
export interface OidcSignInRequest {
  id_token: string;
  organization_id?: string | null;
}

export type SignInRequest = DevSignInRequest | OidcSignInRequest;

export const api = {
  async health(signal?: AbortSignal) {
    return request("/health", z.object({ status: z.string(), version: z.string() }), {
      ...(signal ? { signal } : {}),
    });
  },

  async currentUser(signal?: AbortSignal): Promise<CurrentUser> {
    return request("/auth/me", currentUserSchema, signal ? { signal } : {});
  },

  async refreshCsrf(): Promise<string> {
    const result = await request(
      "/auth/csrf",
      z.object({ csrf_token: z.string() }),
    );
    return result.csrf_token;
  },

  async signIn(payload: SignInRequest): Promise<CurrentUser> {
    await request("/auth/session", z.unknown(), { method: "POST", body: payload });
    return api.currentUser();
  },

  async signOut(): Promise<void> {
    await request("/auth/logout", z.unknown(), { method: "POST" });
  },

  async overview(signal?: AbortSignal): Promise<Overview> {
    return request("/overview", overviewSchema, signal ? { signal } : {});
  },

  async connectors(signal?: AbortSignal): Promise<Connector[]> {
    return request("/connectors", connectorListSchema, signal ? { signal } : {});
  },

  async connector(connectorType: string, signal?: AbortSignal): Promise<Connector> {
    return request(
      `/connectors/${encodeURIComponent(connectorType)}`,
      connectorListSchema.element,
      signal ? { signal } : {},
    );
  },

  async connections(
    params: { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ): Promise<ConnectionList> {
    return request("/connections", connectionListSchema, {
      query: { limit: params.limit, offset: params.offset },
      ...(signal ? { signal } : {}),
    });
  },

  async connection(id: string, signal?: AbortSignal): Promise<Connection> {
    return request(
      `/connections/${encodeURIComponent(id)}`,
      connectionSchema,
      signal ? { signal } : {},
    );
  },

  async createConnection(payload: ConnectionCreateRequest): Promise<Connection> {
    return request("/connections", connectionSchema, { method: "POST", body: payload });
  },

  async updateConnection(
    id: string,
    payload: Partial<ConnectionCreateRequest> & { enabled?: boolean },
  ): Promise<Connection> {
    return request(`/connections/${encodeURIComponent(id)}`, connectionSchema, {
      method: "PATCH",
      body: payload,
    });
  },

  async deleteConnection(id: string): Promise<void> {
    return request(`/connections/${encodeURIComponent(id)}`, z.void(), { method: "DELETE" });
  },

  async testConnection(id: string): Promise<ConnectionTestResult> {
    return request(
      `/connections/${encodeURIComponent(id)}/test`,
      connectionTestResultSchema,
      { method: "POST" },
    );
  },

  async startSync(id: string): Promise<SyncAccepted> {
    return request(`/connections/${encodeURIComponent(id)}/sync`, syncAcceptedSchema, {
      method: "POST",
    });
  },

  async cancelSync(id: string, jobId: string): Promise<SyncJob> {
    return request(
      `/connections/${encodeURIComponent(id)}/sync/${encodeURIComponent(jobId)}/cancel`,
      syncJobSchema,
      { method: "POST" },
    );
  },

  async connectionStatus(id: string, signal?: AbortSignal): Promise<ConnectionStatus> {
    return request(
      `/connections/${encodeURIComponent(id)}/status`,
      connectionStatusSchema,
      signal ? { signal } : {},
    );
  },

  async connectionJobs(
    id: string,
    params: { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ): Promise<SyncJobList> {
    return request(`/connections/${encodeURIComponent(id)}/jobs`, syncJobListSchema, {
      query: { limit: params.limit, offset: params.offset },
      ...(signal ? { signal } : {}),
    });
  },

  async connectionRecords(
    id: string,
    params: { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ): Promise<SourceRecordList> {
    return request(
      `/connections/${encodeURIComponent(id)}/records`,
      sourceRecordListSchema,
      {
        query: { limit: params.limit, offset: params.offset },
        ...(signal ? { signal } : {}),
      },
    );
  },

  async auditEvents(
    params: { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ): Promise<AuditEventList> {
    return request("/audit-events", auditEventListSchema, {
      query: { limit: params.limit, offset: params.offset },
      ...(signal ? { signal } : {}),
    });
  },
};
