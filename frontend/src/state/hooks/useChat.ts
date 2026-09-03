import { useCallback, useEffect } from "react";
import { getHistoryTurns } from "../../api/rest";
import { connections } from "../../api/ws/channels";
import type { AssistantChunkMessage, VoiceTurnMessage } from "../../api/ws/types";
import { CHAT_HISTORY_LOAD_TIMEOUT_MS } from "../../config/runtime";
import { useAppStore } from "../store";

export interface ChatActions {
  sendMessage: (text: string) => boolean;
  cancel: () => void;
}

export function useChat(): ChatActions {
  useEffect(() => {
    let active = true;
    let historyStarted = false;
    let historyTimer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    useAppStore.getState().setChatHistoryLoading(true);

    const loadHistory = () => {
      if (historyStarted) return;
      historyStarted = true;
      historyTimer = setTimeout(() => {
        controller.abort();
        if (active) useAppStore.getState().setChatHistoryLoading(false);
      }, CHAT_HISTORY_LOAD_TIMEOUT_MS);
      void getHistoryTurns(50, controller.signal)
        .then(({ turns }) => {
          if (active && !controller.signal.aborted && Array.isArray(turns)) {
            useAppStore.getState().hydrateHistory(turns);
          }
        })
        .catch(() => undefined)
        .finally(() => {
          clearTimeout(historyTimer);
          if (active) useAppStore.getState().setChatHistoryLoading(false);
        });
    };

    const removeMessage = connections.onMessage(({ channel, message }) => {
      if (channel !== "chat") return;
      const state = useAppStore.getState();
      state.markChannelMessage("chat");
      if (message.type === "assistant_chunk") {
        state.receiveAssistant(message as AssistantChunkMessage);
      } else if (message.type === "voice_turn") {
        const turn = (message as VoiceTurnMessage).data;
        if (turn.kind === "user") {
          state.appendVoiceUserTurn(turn.turn_id, turn.text, turn.ts);
        } else {
          state.appendVoiceReplyTurn(
            turn.turn_id,
            turn.reply_to_turn_id,
            turn.text,
            turn.ts,
          );
        }
      }
    });
    const removeState = connections.onState(({ channel, state }) => {
      if (channel !== "chat") return;
      const store = useAppStore.getState();
      store.setChannelState("chat", state);
      if (state === "open") loadHistory();
      if (state === "reconnecting" || state === "closed") store.failPendingOnDisconnect();
      if (state === "open" && store.ignoreGeneration !== null) store.finishLocalCancel();
      if (state === "open" && !store.pending && store.ignoreGeneration === null) {
        useAppStore.setState({ sendLocked: false });
      }
    });
    connections.connect("chat");
    return () => {
      active = false;
      clearTimeout(historyTimer);
      controller.abort();
      useAppStore.getState().failPendingOnDisconnect();
      useAppStore.getState().setChatHistoryLoading(false);
      removeMessage();
      removeState();
      connections.disconnect("chat");
    };
  }, []);

  const sendMessage = useCallback((rawText: string) => {
    const text = rawText.trim();
    const state = useAppStore.getState();
    if (!text || state.chatHistoryLoading || state.sendLocked
      || connections.state("chat") !== "open") return false;
    state.beginRequest(text);
    const sent = connections.send("chat", { text, session_id: "desktop" });
    if (!sent) state.failPendingOnDisconnect();
    return sent;
  }, []);

  const cancel = useCallback(() => {
    const cancelled = useAppStore.getState().cancelLocal();
    if (cancelled !== null) connections.restart("chat");
  }, []);

  return { sendMessage, cancel };
}
