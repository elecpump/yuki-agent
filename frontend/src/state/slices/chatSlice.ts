import type { StateCreator } from "zustand";
import type { AssistantChunkMessage } from "../../api/ws/types";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  createdAt: number;
  status?: string;
  reason?: string;
  emotion?: string | null;
}

export interface ChatSlice {
  messages: ChatMessage[];
  pending: boolean;
  sendLocked: boolean;
  requestGeneration: number;
  ignoreGeneration: number | null;
  nextRequestMayQueue: boolean;
  requestMayBeQueued: boolean;
  chatError: string | null;
  beginRequest: (text: string) => number;
  receiveAssistant: (message: AssistantChunkMessage) => void;
  cancelLocal: () => number | null;
  finishLocalCancel: () => void;
  failPendingOnDisconnect: () => void;
}

function messageId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

export const createChatSlice: StateCreator<ChatSlice, [], [], ChatSlice> = (set, get) => ({
  messages: [],
  pending: false,
  sendLocked: false,
  requestGeneration: 0,
  ignoreGeneration: null,
  nextRequestMayQueue: false,
  requestMayBeQueued: false,
  chatError: null,

  beginRequest: (text) => {
    const generation = get().requestGeneration + 1;
    set((state) => ({
      messages: [
        ...state.messages,
        { id: messageId(), role: "user", text, createdAt: Date.now() },
      ],
      pending: true,
      sendLocked: true,
      requestGeneration: generation,
      ignoreGeneration: null,
      requestMayBeQueued: state.nextRequestMayQueue,
      nextRequestMayQueue: false,
      chatError: null,
    }));
    return generation;
  },

  receiveAssistant: (message) => {
    if (get().ignoreGeneration !== null) return;
    const text = message.text.trim();
    set((state) => ({
      messages:
        text || message.status === "failed"
          ? [
              ...state.messages,
              {
                id: messageId(),
                role: "assistant",
                text: text || message.error || "回复失败",
                createdAt: message.ts ? message.ts * 1000 : Date.now(),
                status: message.status,
                reason: message.reason,
                emotion: message.emotion,
              },
            ]
          : state.messages,
      pending: false,
      sendLocked: false,
      requestMayBeQueued: false,
      chatError: message.status === "failed" ? message.error || "回复失败" : null,
    }));
  },

  cancelLocal: () => {
    const state = get();
    if (!state.pending) return null;
    set({
      pending: false,
      sendLocked: true,
      ignoreGeneration: state.requestGeneration,
      nextRequestMayQueue: true,
      requestMayBeQueued: false,
      chatError: null,
    });
    return state.requestGeneration;
  },

  finishLocalCancel: () => set({ sendLocked: false, ignoreGeneration: null }),

  failPendingOnDisconnect: () => {
    if (!get().pending) return;
    set({
      pending: false,
      sendLocked: true,
      requestMayBeQueued: false,
      chatError: "连接中断，回复可能已执行但结果无法恢复。连接恢复后可手动重试。",
    });
  },
});
