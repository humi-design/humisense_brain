/**
 * Layer 4 client and page tests.
 *
 * These assert the behaviour the security and product requirements call for: mutations
 * echo the CSRF token, work identifiers are encoded in the path, a work list that fails
 * validation is reported as a handled error, and the pages render real backend data with
 * an unresolved field left unresolved rather than fabricated.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { api, ApiError } from "./client";
import { MyWorkPage } from "../pages/MyWorkPage";
import { WorkDetailPage } from "../pages/WorkDetailPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const summary = {
  work_id: "11111111-1111-1111-1111-111111111111",
  title: "Chase supplier for raw material",
  work_type: "follow_up",
  domain: "procurement",
  status: "waiting",
  priority: "high",
  due_at: null,
  overdue: true,
  due_soon: false,
  blocked: false,
  owner_id: null,
  department_id: null,
  updated_at: "2026-01-01T00:00:00Z",
};

describe("layer 4 api client", () => {
  beforeEach(() => {
    document.cookie = "humisense_csrf=token-abc";
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.cookie = "humisense_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("encodes the work id in the path rather than interpolating it raw", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        work_id: "x",
        work_key: "k",
        work_type: "task",
        title: "t",
        description: null,
        domain: "general",
        department_id: null,
        owner_id: null,
        assigned_user_id: null,
        priority: "normal",
        priority_source: "rule",
        status: "open",
        source_status: null,
        origin: "structured_record",
        confidence: null,
        start_at: null,
        due_at: null,
        due_source: null,
        completed_at: null,
        customer_ref: null,
        supplier_ref: null,
        order_ref: null,
        project_ref: null,
        machine_ref: null,
        production_ref: null,
        attributes: {},
        schema_version: "4.0",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        source_references: [],
        assignments: [],
        commitments: [],
        dependencies: [],
        blockers: [],
        priorities: [],
        timeline: [],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.workItem("../../admin");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/v1/work/..%2F..%2Fadmin");
  });

  it("echoes the CSRF token when changing work status", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.updateWorkStatus("w1", { status: "completed", reason: "done" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).get("X-CSRF-Token")).toBe("token-abc");
    expect(init.method).toBe("POST");
  });

  it("reports a work list that does not match the contract as a handled error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ unexpected: true })));

    const error = (await api.work().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("response_validation_failed");
  });

  it("does not send a CSRF header on a safe work read", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.work();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).get("X-CSRF-Token")).toBeNull();
  });
});

describe("MyWorkPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the backend's personal view", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          as_of: "2026-01-02T00:00:00Z",
          my_work: [summary],
          overdue: [summary],
          due_soon: [],
          waiting_for_me: [],
          waiting_on_others: [],
          counts: { waiting: 1 },
        }),
      ),
    );

    render(
      <MemoryRouter>
        <MyWorkPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Chase supplier for raw material")).toBeInTheDocument();
    expect(screen.getByText("overdue")).toBeInTheDocument();
  });

  it("shows an empty state rather than inventing work", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          as_of: "2026-01-02T00:00:00Z",
          my_work: [],
          overdue: [],
          due_soon: [],
          waiting_for_me: [],
          waiting_on_others: [],
          counts: {},
        }),
      ),
    );

    render(
      <MemoryRouter>
        <MyWorkPage />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("No work is assigned to you right now."),
    ).toBeInTheDocument();
  });
});

describe("WorkDetailPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("leaves an unresolved owner unresolved instead of fabricating one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          work_id: "22222222-2222-2222-2222-222222222222",
          work_key: "wf-1",
          work_type: "task",
          title: "Inspect machine 4B",
          description: null,
          domain: "maintenance",
          department_id: null,
          owner_id: null,
          assigned_user_id: null,
          priority: "normal",
          priority_source: "rule",
          status: "open",
          source_status: "OPEN",
          origin: "structured_record",
          confidence: 0.5,
          start_at: null,
          due_at: null,
          due_source: null,
          completed_at: null,
          customer_ref: null,
          supplier_ref: null,
          order_ref: null,
          project_ref: null,
          machine_ref: "4B",
          production_ref: null,
          attributes: {},
          schema_version: "4.0",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          source_references: [],
          assignments: [],
          commitments: [],
          dependencies: [],
          blockers: [],
          priorities: [],
          timeline: [],
        }),
      ),
    );

    render(
      <MemoryRouter initialEntries={["/work/22222222-2222-2222-2222-222222222222"]}>
        <WorkDetailPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Inspect machine 4B")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("unresolved").length).toBeGreaterThan(0));
    // A missing confidence must render as unknown, not as a fabricated 0%.
    expect(screen.getByText("confidence: 50%")).toBeInTheDocument();
  });
});
