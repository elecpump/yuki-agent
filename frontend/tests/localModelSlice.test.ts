import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../src/state/store";

describe("local model slice", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    useAppStore.setState({
      localModelStatus: null,
      localModelLoading: false,
      localModelOperationId: null,
      localModelError: null,
    });
  });

  it("refreshes the authoritative model status", async () => {
    const status = {
      available: true,
      enabled: false,
      target_enabled: false,
      state: "disabled",
      runtime_state: "disabled",
      loaded: false,
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

    const refresh = useAppStore.getState().refreshLocalModel;
    expect(useAppStore.getState().localModelLoading).toBe(false);
    const pending = refresh();
    expect(useAppStore.getState().localModelLoading).toBe(true);
    await pending;

    expect(useAppStore.getState()).toMatchObject({
      localModelStatus: status,
      localModelLoading: false,
      localModelError: null,
    });
  });

  it("stores the accepted operation without changing status optimistically", async () => {
    const current = {
      available: true,
      enabled: true,
      target_enabled: true,
      state: "enabled" as const,
      runtime_state: "ready",
      loaded: true,
      active_calls: 0,
      operation: null,
      last_error: "",
    };
    useAppStore.setState({ localModelStatus: current });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ operation_id: "op-disable", accepted: true, target_enabled: false }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await useAppStore.getState().setLocalModelEnabled(false, "request-disable");

    expect(useAppStore.getState()).toMatchObject({
      localModelStatus: current,
      localModelOperationId: "op-disable",
      localModelLoading: true,
      localModelError: null,
    });
  });

  it("tracks and finishes a local model operation", () => {
    useAppStore.getState().trackLocalModelOperation("op-recover", "正在恢复连接");

    expect(useAppStore.getState()).toMatchObject({
      localModelLoading: true,
      localModelOperationId: "op-recover",
      localModelError: "正在恢复连接",
    });

    useAppStore.getState().finishLocalModelOperation("load_failed");

    expect(useAppStore.getState()).toMatchObject({
      localModelLoading: false,
      localModelOperationId: null,
      localModelError: "load_failed",
    });
  });
});
