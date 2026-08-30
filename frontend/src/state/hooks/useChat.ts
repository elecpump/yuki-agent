import { useCallback, useEffect } from "react";
import { connections } from "../../api/ws/channels";
import type { AssistantChunkMessage } from "../../api/ws/types";
import { useAppStore } from "../store";

export interface ChatActions {
  sendMessage: (text: string) => boolean;
  cancel: () => void;
}

export function useChat(): ChatActions {
  useEffect(() => {
    const removeMessage = connections.onMessage(({ channel, message }) => {
      if (channel !== "chat") return;
      const state = useAppStore.getState();
      state.markChannelMessage("chat");
      if (message.type === "assistant_chunk") {
        state.receiveAssistant(message as AssistantChunkMessage);
      }
    });
    const removeState = connections.onState(({ channel, state }) => {
      if (channel !== "chat") return;
      const store = useAppStore.getState();
      store.setChannelState("chat", state);
      if (state === "reconnecting" || state === "closed") store.failPendingOnDisconnect();
      if (state === "open" && store.ignoreGeneration !== null) store.finishLocalCancel();
      if (state === "open" && !store.pending && store.ignoreGeneration === null) {
        useAppStore.setState({ sendLocked: false });
      }
    });
    connections.connect("chat");
    return () => {
      removeMessage();
      removeState();
      connections.disconnect("chat");
    };
  }, []);

  const sendMessage = useCallback((rawText: string) => {
    const text = rawText.trim();
    const state = useAppStore.getState();
    if (!text || state.sendLocked || connections.state("chat") !== "open") return false;
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
