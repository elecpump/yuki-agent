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
      user_turn_id: 10,
      turn_id: 11,
      done: true,
      status: "completed",
    });

    const state = useAppStore.getState();
    expect(state.pending).toBe(false);
    expect(state.sendLocked).toBe(false);
    expect(state.messages.map((message) => message.text)).toEqual(["你好", "晚上好"]);
    expect(state.messages.map((message) => message.turnId)).toEqual([10, 11]);
    expect(state.messages[1]?.replyToTurnId).toBe(10);
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

  it("hydrates history chronologically and deduplicates overlapping voice turns", () => {
    useAppStore.getState().hydrateHistory([
      { id: 2, role: "agent", source: "agent_reply", content: "回答", ts: 2 },
      { id: 1, role: "user", source: "user_input", content: "问题", ts: 1 },
    ]);
    useAppStore.getState().appendVoiceReplyTurn(2, 1, "重复回答", 2);
    useAppStore.getState().appendVoiceUserTurn(3, "继续问", 3);

    expect(useAppStore.getState().messages.map(({ turnId, role, text }) => ({
      turnId,
      role,
      text,
    }))).toEqual([
      { turnId: 1, role: "user", text: "问题" },
      { turnId: 2, role: "assistant", text: "回答" },
      { turnId: 3, role: "user", text: "继续问" },
    ]);
  });

  it("preserves local messages without persisted turn ids while hydrating history", () => {
    useAppStore.setState({
      messages: [{
        id: "local-user",
        role: "user",
        text: "正在输入的消息",
        createdAt: 3_000,
      }],
    });

    useAppStore.getState().hydrateHistory([
      { id: 1, role: "user", source: "user_input", content: "历史消息", ts: 1 },
    ]);

    expect(useAppStore.getState().messages.map((message) => message.text)).toEqual([
      "历史消息",
      "正在输入的消息",
    ]);
  });
});
