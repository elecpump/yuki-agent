import { afterEach, describe, expect, it, vi } from "vitest";
import { requestJson } from "../src/api/client";

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
