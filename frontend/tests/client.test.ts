import { afterEach, describe, expect, it, vi } from "vitest";
import { requestJson } from "../src/api/client";
import {
  getLocalModelOperation,
  getLocalModelStatus,
  setLocalModelEnabled,
} from "../src/api/rest";

describe("REST client errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("accepts open error codes from the gateway envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error: { code: "future_code", message: "nope", details: { x: 1 } } }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(requestJson("/future")).rejects.toMatchObject({
      code: "future_code",
      message: "nope",
      status: 409,
    });
  });

  it("normalizes FastAPI validation details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: [{ msg: "Field required" }] }), { status: 422 }),
      ),
    );

    await expect(requestJson("/invalid")).rejects.toMatchObject({
      code: "validation_error",
      message: "Field required",
    });
  });
});

describe("local model REST client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the authoritative local model status", async () => {
    const status = {
      available: true,
      enabled: true,
      target_enabled: true,
      state: "enabled",
      runtime_state: "ready",
      loaded: true,
      active_calls: 0,
      operation: null,
      last_error: "",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(status), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(getLocalModelStatus()).resolves.toEqual(status);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/local-model$/),
      expect.objectContaining({ headers: expect.objectContaining({ Accept: "application/json" }) }),
    );
  });

  it("submits a target and reads its operation", async () => {
    const accepted = { operation_id: "op-1", accepted: true, target_enabled: false };
    const operation = {
      operation_id: "op-1",
      target_enabled: false,
      state: "running",
      error_code: null,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(accepted), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(operation), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(setLocalModelEnabled(false, "request-1")).resolves.toEqual(accepted);
    await expect(getLocalModelOperation("op-1")).resolves.toEqual(operation);
    expect(fetchMock.mock.calls[0]).toEqual([
      expect.stringMatching(/\/api\/local-model$/),
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ enabled: false, idempotency_key: "request-1" }),
      }),
    ]);
    expect(fetchMock.mock.calls[1]?.[0]).toMatch(/\/api\/local-model\/operations\/op-1$/);
  });
});
