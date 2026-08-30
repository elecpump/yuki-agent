import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../src/state/store";

describe("chat slice", () => {
  beforeEach(() => {
    useAppStore.setState({
      messages: [],
      pending: false,
      sendLocked: false,
      requestGeneration: 0,
      ignoreGeneration: null,
      nextRequestMayQueue: false,
      requestMayBeQueued: false,
      chatError: null,
    });
  });

  it("starts and completes a request", () => {
    const generation = useAppStore.getState().beginRequest("你好");
    expect(generation).toBe(1);
    expect(useAppStore.getState()).toMatchObject({ pending: true, sendLocked: true });

    useAppStore.getState().receiveAssistant({
      type: "assistant_chunk",
      task_id: "server-task",
      text: "晚上好",
      reason: "chat_local",
      done: true,
      status: "completed",
    });

    const state = useAppStore.getState();
    expect(state.pending).toBe(false);
    expect(state.sendLocked).toBe(false);
    expect(state.messages.map((message) => message.text)).toEqual(["你好", "晚上好"]);
    expect(state.messages[1]?.reason).toBe("chat_local");
  });

  it("locks sending and ignores replies while a local cancel is rebuilding the socket", () => {
    useAppStore.getState().beginRequest("慢请求");
    expect(useAppStore.getState().cancelLocal()).toBe(1);

    useAppStore.getState().receiveAssistant({
      type: "assistant_chunk",
      task_id: "late",
      text: "迟到回复",
      done: true,
      status: "completed",
    });

    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().sendLocked).toBe(true);
    useAppStore.getState().finishLocalCancel();
    expect(useAppStore.getState().sendLocked).toBe(false);
  });

  it("does not retry a pending request after disconnect", () => {
    useAppStore.getState().beginRequest("会丢失吗");
    useAppStore.getState().failPendingOnDisconnect();
    expect(useAppStore.getState()).toMatchObject({ pending: false, sendLocked: true });
    expect(useAppStore.getState().chatError).toContain("结果无法恢复");
  });

  it("marks the first request after local cancel as potentially queued", () => {
    useAppStore.getState().beginRequest("旧请求");
    useAppStore.getState().cancelLocal();
    useAppStore.getState().finishLocalCancel();
    useAppStore.getState().beginRequest("新请求");

    expect(useAppStore.getState().requestMayBeQueued).toBe(true);
    expect(useAppStore.getState().nextRequestMayQueue).toBe(false);
  });
});
