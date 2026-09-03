import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChat } from "../src/state/hooks/useChat";
import { CHAT_HISTORY_LOAD_TIMEOUT_MS } from "../src/config/runtime";
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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ turns: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )));
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
    await act(async () => undefined);
    act(() => expect(result.current.sendMessage("你好")).toBe(true));
    expect(sockets[0]?.sent).toHaveLength(1);

    act(() => sockets[0]?.close());
    await act(async () => vi.advanceTimersByTimeAsync(500));

    expect(sockets.flatMap((socket) => socket.sent)).toHaveLength(1);
    expect(useAppStore.getState().pending).toBe(false);
    expect(useAppStore.getState().chatError).toContain("结果无法恢复");
  });

  it("rebuilds the socket on cancel and isolates a late old reply", async () => {
    const { result } = renderHook(() => useChat());
    const oldSocket = sockets[0]!;
    act(() => oldSocket.open());
    await act(async () => undefined);
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

  it("appends live voice turns without entering the text request state", () => {
    renderHook(() => useChat());
    act(() => sockets[0]?.open());
    act(() => sockets[0]?.message({
      type: "voice_turn",
      data: {
        kind: "user",
        turn_id: 4,
        reply_to_turn_id: null,
        text: "语音输入",
        ts: 4,
      },
    }));

    expect(useAppStore.getState().messages[0]).toMatchObject({
      turnId: 4,
      role: "user",
      text: "语音输入",
    });
    expect(useAppStore.getState()).toMatchObject({ pending: false, sendLocked: false });
  });

  it("blocks text during history loading while receiving and deduplicating voice turns", async () => {
    let resolveHistory: (response: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => {
      resolveHistory = resolve;
    })));
    const { result } = renderHook(() => useChat());
    expect(sockets).toHaveLength(1);
    act(() => sockets[0]?.open());
    act(() => expect(result.current.sendMessage("正在输入")).toBe(false));
    expect(useAppStore.getState().pending).toBe(false);
    act(() => sockets[0]?.message({
      type: "voice_turn",
      data: { kind: "user", turn_id: 2, reply_to_turn_id: null, text: "实时语音", ts: 2 },
    }));

    await act(async () => {
      resolveHistory(new Response(JSON.stringify({
        turns: [
          { id: 2, role: "user", source: "user_input", content: "实时语音", ts: 2 },
          { id: 1, role: "user", source: "user_input", content: "历史消息", ts: 1 },
        ],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
      await Promise.resolve();
    });
    expect(useAppStore.getState().messages.map((message) => message.text)).toEqual([
      "历史消息", "实时语音",
    ]);
    act(() => expect(result.current.sendMessage("正在输入")).toBe(true));

    await act(async () => {
      sockets[0]?.message({
        type: "assistant_chunk",
        task_id: "current",
        text: "",
        error: "回复失败",
        done: true,
        status: "failed",
      });
      await Promise.resolve();
    });

    expect(useAppStore.getState().messages.map((message) => message.text)).toEqual([
      "历史消息",
      "实时语音",
      "正在输入",
      "回复失败",
    ]);
  });

  it("allows text chat when history loading fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("history unavailable")));
    const { result } = renderHook(() => useChat());
    act(() => sockets[0]?.open());
    await act(async () => undefined);

    act(() => expect(result.current.sendMessage("继续聊天")).toBe(true));
  });

  it("unlocks after a history timeout and ignores a late history response", async () => {
    let resolveHistory: (response: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => {
      resolveHistory = resolve;
    })));
    const { result } = renderHook(() => useChat());
    act(() => sockets[0]?.open());
    await act(async () => vi.advanceTimersByTimeAsync(CHAT_HISTORY_LOAD_TIMEOUT_MS));
    act(() => expect(result.current.sendMessage("新请求")).toBe(true));

    await act(async () => {
      resolveHistory(new Response(JSON.stringify({
        turns: [{ id: 1, role: "user", source: "user_input", content: "新请求", ts: 1 }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    });
    expect(useAppStore.getState().messages.map((message) => message.text)).toEqual(["新请求"]);
    expect(useAppStore.getState().chatHistoryLoading).toBe(false);
  });

  it("ignores a history response after unmount", async () => {
    let resolveHistory: (response: Response) => void = () => undefined;
    const fetchMock = vi.fn().mockReturnValue(new Promise<Response>((resolve) => {
      resolveHistory = resolve;
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useChat());
    act(() => sockets[0]?.open());
    unmount();

    expect(fetchMock.mock.calls[0]?.[1]?.signal.aborted).toBe(true);
    await act(async () => {
      resolveHistory(new Response(JSON.stringify({
        turns: [{ id: 1, role: "user", source: "user_input", content: "迟到历史", ts: 1 }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
      vi.advanceTimersByTime(CHAT_HISTORY_LOAD_TIMEOUT_MS);
    });
    expect(useAppStore.getState().messages).toEqual([]);
    expect(sockets).toHaveLength(1);
  });
});
