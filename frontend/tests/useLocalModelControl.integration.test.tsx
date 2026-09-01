import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLocalModelControl } from "../src/state/hooks/useLocalModelControl";
import { useAppStore } from "../src/state/store";

const enabledStatus = {
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

describe("useLocalModelControl integration", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useAppStore.setState({
      localModelStatus: null,
      localModelLoading: false,
      localModelOperationId: null,
      localModelError: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("polls an accepted operation and refreshes authoritative status after success", async () => {
    const disabledStatus = {
      ...enabledStatus,
      enabled: false,
      target_enabled: false,
      state: "disabled",
      runtime_state: "disabled",
      loaded: false,
    };
    const response = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(enabledStatus))
      .mockResolvedValueOnce(
        response({ operation_id: "op-1", accepted: true, target_enabled: false }, 202),
      )
      .mockResolvedValueOnce(
        response({ operation_id: "op-1", target_enabled: false, state: "queued", error_code: null }),
      )
      .mockResolvedValueOnce(
        response({ operation_id: "op-1", target_enabled: false, state: "running", error_code: null }),
      )
      .mockResolvedValueOnce(
        response({ operation_id: "op-1", target_enabled: false, state: "succeeded", error_code: null }),
      )
      .mockResolvedValueOnce(response(disabledStatus));
    vi.stubGlobal("fetch", fetchMock);
    const randomUUID = vi.fn(() => "request-uuid");
    vi.stubGlobal("crypto", { randomUUID });

    const { result } = renderHook(() => useLocalModelControl());
    await act(async () => undefined);
    expect(result.current.status?.enabled).toBe(true);

    await act(async () => result.current.setEnabled(false));
    expect(randomUUID).toHaveBeenCalledOnce();
    expect(result.current.loading).toBe(true);

    await act(async () => vi.advanceTimersByTimeAsync(1_500));

    expect(result.current.status).toEqual(disabledStatus);
    expect(result.current.loading).toBe(false);
    expect(result.current.operationId).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it("stops operation polling after unmount", async () => {
    const response = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(enabledStatus))
      .mockResolvedValueOnce(
        response({ operation_id: "op-2", accepted: true, target_enabled: false }, 202),
      );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "request-2" });

    const { result, unmount } = renderHook(() => useLocalModelControl());
    await act(async () => undefined);
    await act(async () => result.current.setEnabled(false));
    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(2_000));

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("refreshes authoritative status and preserves the operation error", async () => {
    const failedStatus = {
      ...enabledStatus,
      enabled: false,
      target_enabled: true,
      state: "failed",
      loaded: false,
      last_error: "load_failed",
    };
    const response = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(enabledStatus))
      .mockResolvedValueOnce(
        response({ operation_id: "op-3", accepted: true, target_enabled: false }, 202),
      )
      .mockResolvedValueOnce(
        response({
          operation_id: "op-3",
          target_enabled: false,
          state: "failed",
          error_code: "load_failed",
        }),
      )
      .mockResolvedValueOnce(response(failedStatus));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "request-3" });

    const { result } = renderHook(() => useLocalModelControl());
    await act(async () => undefined);
    await act(async () => result.current.setEnabled(false));
    await act(async () => vi.advanceTimersByTimeAsync(500));

    expect(result.current.status).toEqual(failedStatus);
    expect(result.current.error).toBe("load_failed");
    expect(result.current.loading).toBe(false);
  });

  it("continues polling when a transient poll failure refreshes an active operation", async () => {
    const activeStatus = {
      ...enabledStatus,
      enabled: false,
      state: "disabling",
      operation: {
        operation_id: "op-transient",
        target_enabled: false,
        state: "running",
        error_code: null,
      },
    };
    const disabledStatus = {
      ...enabledStatus,
      enabled: false,
      target_enabled: false,
      state: "disabled",
      runtime_state: "disabled",
      loaded: false,
    };
    const response = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(enabledStatus))
      .mockResolvedValueOnce(
        response({ operation_id: "op-transient", accepted: true, target_enabled: false }, 202),
      )
      .mockRejectedValueOnce(new Error("temporary network failure"))
      .mockResolvedValueOnce(response(activeStatus))
      .mockResolvedValueOnce(
        response({
          operation_id: "op-transient",
          target_enabled: false,
          state: "succeeded",
          error_code: null,
        }),
      )
      .mockResolvedValueOnce(response(disabledStatus));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "request-transient" });

    const { result } = renderHook(() => useLocalModelControl());
    await act(async () => undefined);
    await act(async () => result.current.setEnabled(false));
    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(result.current.status).toEqual(disabledStatus);
    expect(result.current.loading).toBe(false);
  });

  it("resumes polling an operation discovered during the initial status refresh", async () => {
    const inProgressStatus = {
      ...enabledStatus,
      enabled: false,
      state: "enabling",
      loaded: false,
      operation: {
        operation_id: "op-existing",
        target_enabled: true,
        state: "running",
        error_code: null,
      },
    };
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(inProgressStatus))
      .mockResolvedValueOnce(
        response({
          operation_id: "op-existing",
          target_enabled: true,
          state: "succeeded",
          error_code: null,
        }),
      )
      .mockResolvedValueOnce(response(enabledStatus));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useLocalModelControl());
    await act(async () => undefined);
    await act(async () => vi.advanceTimersByTimeAsync(500));

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.status).toEqual(enabledStatus);
  });

  it("does not poll a stale operation when the initial status refresh fails", async () => {
    useAppStore.setState({
      localModelStatus: {
        ...enabledStatus,
        enabled: false,
        state: "enabling",
        operation: {
          operation_id: "stale-operation",
          target_enabled: true,
          state: "running",
          error_code: null,
        },
      },
    });
    const fetchMock = vi.fn().mockRejectedValue(new Error("Gateway 不可达"));
    vi.stubGlobal("fetch", fetchMock);

    renderHook(() => useLocalModelControl());
    await act(async () => undefined);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState().localModelError).toBe("Gateway 不可达");
  });
});
