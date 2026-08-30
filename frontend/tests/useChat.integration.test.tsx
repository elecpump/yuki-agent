import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChat } from "../src/state/hooks/useChat";
import { useAppStore } from "../src/state/store";
import { FakeWebSocket } from "./FakeWebSocket";

describe("useChat integration", () => {
  const sockets: FakeWebSocket[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    sockets.length = 0;
    vi.stubGlobal(
      "WebSocket",
      class extends FakeWebSocket {
        constructor(url: string) {
          super(url);
          sockets.push(this);
        }
      },
    );
    useAppStore.setState({
      messages: [],
      pending: false,
      sendLocked: false,
      requestGeneration: 0,
      ignoreGeneration: null,
      nextRequestMayQueue: false,
      requestMayBeQueued: false,
      chatError: null,
      chatWsState: "idle",
      chatLastMessageAt: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("marks a pending request failed on disconnect without resending it", async () => {
    const { result } = renderHook(() => useChat());
    act(() => sockets[0]?.open());
    act(() => expect(result.current.sendMessage("你好")).toBe(true));
    expect(sockets[0]?.sent).toHaveLength(1);

    act(() => sockets[0]?.close());
    await act(async () => vi.advanceTimersByTimeAsync(500));

    expect(sockets.flatMap((socket) => socket.sent)).toHaveLength(1);
    expect(useAppStore.getState().pending).toBe(false);
    expect(useAppStore.getState().chatError).toContain("结果无法恢复");
  });

  it("rebuilds the socket on cancel and isolates a late old reply", () => {
    const { result } = renderHook(() => useChat());
    const oldSocket = sockets[0]!;
    act(() => oldSocket.open());
    act(() => result.current.sendMessage("慢请求"));
    act(() => result.current.cancel());
    const newSocket = sockets[1]!;

    act(() => {
      oldSocket.message({
        type: "assistant_chunk",
        task_id: "late",
        text: "迟到回复",
        done: true,
        status: "completed",
      });
      newSocket.open();
    });

    expect(useAppStore.getState().messages.map((message) => message.text)).toEqual(["慢请求"]);
    expect(useAppStore.getState().sendLocked).toBe(false);
    expect(sockets.flatMap((socket) => socket.sent)).toHaveLength(1);
  });
});
