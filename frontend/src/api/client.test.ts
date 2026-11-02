/**
 * Tests for the typed API client.
 *
 * These assert the security-relevant behaviour: the CSRF header is echoed on mutations,
 * errors are parsed without leaking markup, a contract mismatch is reported safely, and
 * no credential is ever written to browser storage.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  beforeEach(() => {
    document.cookie = "humisense_csrf=token-abc";
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.cookie = "humisense_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    localStorage.clear();
    sessionStorage.clear();
  });

  it("echoes the CSRF cookie in a header on a mutating request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        ok: true,
        message: "Reachable",
        error_code: null,
        latency_ms: 4.2,
        details: {},
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.testConnection("11111111-1111-1111-1111-111111111111");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("token-abc");
    expect(init.credentials).toBe("same-origin");
  });

  it("does not send a CSRF header on a safe read", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.connections();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).get("X-CSRF-Token")).toBeNull();
  });

  it("never persists a credential to browser storage", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        user_id: "u1",
        display_name: "Ada",
        email: null,
        organization_id: "o1",
        organization_name: "Acme",
        role: "admin",
        permissions: ["connections:read"],
        auth_method: "dev",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.currentUser();

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("surfaces the backend error envelope as a typed ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "not_found",
              message: "Connection not found",
              correlation_id: "corr-9",
              details: null,
            },
          },
          404,
        ),
      ),
    );

    const error = await api.connection("missing").catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(404);
    expect(apiError.code).toBe("not_found");
    expect(apiError.correlationId).toBe("corr-9");
    expect(apiError.isNotFound).toBe(true);
  });

  it("reports a non-JSON body without exposing it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<html><body>502 Bad Gateway</body></html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );

    const error = (await api.overview().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).not.toContain("<html>");
    expect(error.code).toBe("http_502");
  });

  it("rejects a response that does not match the contract", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ unexpected: true })));

    const error = (await api.overview().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("response_validation_failed");
  });

  it("turns a network failure into a handled error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = (await api.overview().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("network_error");
  });

  it("encodes identifiers in the path rather than interpolating them raw", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: "x",
        connector_type: "csv",
        name: "n",
        description: null,
        config: {},
        has_credentials: false,
        status: "ready",
        enabled: true,
        capabilities: {},
        last_tested_at: null,
        last_success_at: null,
        last_error_code: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.connection("../../admin");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/v1/connections/..%2F..%2Fadmin");
  });
});
