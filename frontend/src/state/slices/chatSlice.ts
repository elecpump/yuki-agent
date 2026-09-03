import type { StateCreator } from "zustand";
import type { AssistantChunkMessage } from "../../api/ws/types";
import type { HistoryTurn } from "../../types/api";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  createdAt: number;
  status?: string;
  reason?: string;
  emotion?: string | null;
  turnId?: number;
  replyToTurnId?: number | null;
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
  chatHistoryLoading: boolean;
  setChatHistoryLoading: (loading: boolean) => void;
  beginRequest: (text: string) => number;
  receiveAssistant: (message: AssistantChunkMessage) => void;
  cancelLocal: () => number | null;
  finishLocalCancel: () => void;
  failPendingOnDisconnect: () => void;
  appendVoiceUserTurn: (turnId: number, text: string, ts: number) => void;
  appendVoiceReplyTurn: (
    turnId: number,
    replyToTurnId: number | null,
    text: string,
    ts: number,
  ) => void;
  hydrateHistory: (turns: HistoryTurn[]) => void;
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
  chatHistoryLoading: false,
  setChatHistoryLoading: (loading) => set({ chatHistoryLoading: loading }),

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
    set((state) => {
      let messages = state.messages;
      if (message.user_turn_id != null) {
        let userIndex = -1;
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          const item = messages[index];
          if (item?.role === "user" && item.turnId === undefined) {
            userIndex = index;
            break;
          }
        }
        if (userIndex >= 0) {
          messages = messages.map((item, index) => (
            index === userIndex ? { ...item, turnId: message.user_turn_id ?? undefined } : item
          ));
        }
      }
      if (text || message.status === "failed") {
        messages = [
          ...messages,
          {
            id: message.turn_id != null ? `turn-${message.turn_id}` : messageId(),
            turnId: message.turn_id ?? undefined,
            replyToTurnId: message.user_turn_id ?? undefined,
            role: "assistant",
            text: text || message.error || "回复失败",
            createdAt: message.ts ? message.ts * 1000 : Date.now(),
            status: message.status,
            reason: message.reason,
            emotion: message.emotion,
          },
        ];
      }
      return {
        messages,
        pending: false,
        sendLocked: false,
        requestMayBeQueued: false,
        chatError: message.status === "failed" ? message.error || "回复失败" : null,
      };
    });
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

  appendVoiceUserTurn: (turnId, text, ts) => set((state) => ({
    messages: state.messages.some((message) => message.turnId === turnId)
      ? state.messages
      : [
          ...state.messages,
          { id: `turn-${turnId}`, turnId, role: "user", text, createdAt: ts * 1000 },
        ],
  })),

  appendVoiceReplyTurn: (turnId, replyToTurnId, text, ts) => {
    set((state) => ({
      messages: state.messages.some((message) => message.turnId === turnId)
        ? state.messages
        : [
            ...state.messages,
            {
              id: `turn-${turnId}`,
              turnId,
              replyToTurnId,
              role: "assistant",
              text,
              createdAt: ts * 1000,
            },
          ],
    }));
  },

  hydrateHistory: (turns) => set((state) => {
    if (state.pending) return {};
    const historyMessages: ChatMessage[] = [...turns].reverse().map((turn) => ({
      id: `turn-${turn.id}`,
      turnId: turn.id,
      role: turn.role === "agent" ? "assistant" : "user",
      text: turn.content,
      createdAt: turn.ts * 1000,
    }));
    const historyIds = new Set(historyMessages.map((message) => message.turnId));
    const liveOnly = state.messages.filter((message) => !historyIds.has(message.turnId));
    return {
      messages: [...historyMessages, ...liveOnly].sort(
        (left, right) => left.createdAt - right.createdAt,
      ),
    };
  }),
});
