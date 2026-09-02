import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVoiceControl } from "../src/state/hooks/useVoiceControl";
import { useAppStore } from "../src/state/store";

const idle = { available: true, state: "idle", session_id: null, active: false };
const listening = { available: true, state: "listening", session_id: 1, active: true };

describe("useVoiceControl integration", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useAppStore.setState({ voiceStatus: null, voicePending: false, voiceError: null });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("starts from the keyboard shortcut and polls until the backend returns idle", async () => {
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(idle))
      .mockResolvedValueOnce(response(listening))
      .mockResolvedValueOnce(response(idle));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);

    await act(async () => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          code: "Space",
          ctrlKey: true,
          shiftKey: true,
          bubbles: true,
        }),
      );
    });
    expect(result.current.status).toEqual(listening);

    await act(async () => vi.advanceTimersByTimeAsync(500));

    expect(result.current.status).toEqual(idle);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps polling while TTS is speaking so the UI observes the return to idle", async () => {
    const tts = { available: true, state: "tts", session_id: null, active: false };
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(tts))
      .mockResolvedValueOnce(response(idle));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);
    expect(result.current.status).toEqual(tts);

    await act(async () => vi.advanceTimersByTimeAsync(500));

    expect(result.current.status).toEqual(idle);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("cancels an active session and ignores repeated shortcut events", async () => {
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(listening))
      .mockResolvedValueOnce(response(idle));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);

    await act(async () => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          code: "Space",
          ctrlKey: true,
          shiftKey: true,
          repeat: true,
        }),
      );
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", { code: "Space", ctrlKey: true, shiftKey: true }),
      );
    });

    expect(result.current.status).toEqual(idle);
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("DELETE");
  });

  it("ignores a late status response after unmount", async () => {
    let resolveRequest: (response: Response) => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        }),
      ),
    );

    const { unmount } = renderHook(() => useVoiceControl());
    unmount();
    await act(async () => {
      resolveRequest(
        new Response(JSON.stringify(listening), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });

    expect(useAppStore.getState()).toMatchObject({
      voiceStatus: null,
      voicePending: false,
      voiceError: null,
    });
  });

  it("continues polling after a transient status failure", async () => {
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(listening))
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValueOnce(response(idle));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(result.current.status).toEqual(idle);
    expect(result.current.error).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps the current poll when a StrictMode setup request resolves late", async () => {
    const resolvers: Array<(response: Response) => void> = [];
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi.fn().mockImplementation(
      () => new Promise<Response>((resolve) => resolvers.push(resolve)),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useVoiceControl(), { reactStrictMode: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => resolvers[1]!(response(listening)));
    expect(result.current.status).toEqual(listening);
    await act(async () => resolvers[0]!(response(idle)));

    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await act(async () => resolvers[2]!(response(idle)));
    expect(result.current.status).toEqual(idle);
  });

  it("does not start a status poll while cancellation is pending", async () => {
    let resolveCancel: (response: Response) => void = () => undefined;
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(listening))
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveCancel = resolve;
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);
    await act(async () => vi.advanceTimersByTimeAsync(499));

    let cancellation: Promise<void> = Promise.resolve();
    act(() => {
      cancellation = result.current.toggleVoice();
    });
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveCancel(response(idle));
      await cancellation;
    });
    expect(result.current.status).toEqual(idle);
  });

  it("removes the shortcut listener and polling timer on unmount", async () => {
    const response = new Response(JSON.stringify(listening), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderHook(() => useVoiceControl());
    await act(async () => undefined);
    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(500));
    window.dispatchEvent(
      new KeyboardEvent("keydown", { code: "Space", ctrlKey: true, shiftKey: true }),
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("deduplicates simultaneous button and shortcut toggles", async () => {
    let resolveStart: (response: Response) => void = () => undefined;
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(idle))
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveStart = resolve;
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);
    let buttonRequest: Promise<void> = Promise.resolve();
    act(() => {
      buttonRequest = result.current.toggleVoice();
      window.dispatchEvent(
        new KeyboardEvent("keydown", { code: "Space", ctrlKey: true, shiftKey: true }),
      );
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => {
      resolveStart(response(listening));
      await buttonRequest;
    });
    expect(result.current.status).toEqual(listening);
  });

  it("cancels while a status poll is in flight and ignores its late result", async () => {
    let resolvePoll: (response: Response) => void = () => undefined;
    const response = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(listening))
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolvePoll = resolve;
        }),
      )
      .mockResolvedValueOnce(response(idle));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useVoiceControl());
    await act(async () => undefined);
    act(() => vi.advanceTimersByTime(500));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.pending).toBe(false);

    await act(async () => result.current.toggleVoice());
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.status).toEqual(idle);

    await act(async () => resolvePoll(response(listening)));
    expect(result.current.status).toEqual(idle);
  });
});
