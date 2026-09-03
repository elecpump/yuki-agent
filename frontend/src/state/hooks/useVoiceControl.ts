import { useCallback, useEffect, useRef } from "react";
import { VOICE_POLL_MS } from "../../config/runtime";
import type { VoiceStatus } from "../../types/api";
import { useAppStore } from "../store";

export interface VoiceControl {
  status: VoiceStatus | null;
  pending: boolean;
  error: string | null;
  toggleVoice: () => Promise<void>;
}

function shouldPoll(status: VoiceStatus | null): boolean {
  return Boolean(status && (status.active || status.state === "tts"));
}

export function useVoiceControl(): VoiceControl {
  const status = useAppStore((state) => state.voiceStatus);
  const pending = useAppStore((state) => state.voicePending);
  const error = useAppStore((state) => state.voiceError);
  const disposed = useRef(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestController = useRef<AbortController | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) clearTimeout(pollTimer.current);
    pollTimer.current = null;
  }, []);

  const poll = useCallback(() => {
    stopPolling();
    pollTimer.current = setTimeout(async () => {
      if (disposed.current) return;
      const controller = new AbortController();
      requestController.current = controller;
      const refreshed = await useAppStore.getState().refreshVoice(controller.signal);
      if (disposed.current) return;
      if (shouldPoll(refreshed ?? useAppStore.getState().voiceStatus)) poll();
    }, VOICE_POLL_MS);
  }, [stopPolling]);

  const updatePolling = useCallback((nextStatus: VoiceStatus | null) => {
    if (shouldPoll(nextStatus)) poll();
    else stopPolling();
  }, [poll, stopPolling]);

  const toggleVoice = useCallback(async () => {
    const state = useAppStore.getState();
    if (state.voicePending || !state.voiceStatus?.available || state.voiceStatus.state === "tts") {
      return;
    }
    stopPolling();
    const controller = new AbortController();
    requestController.current = controller;
    const nextStatus = state.voiceStatus.active
      ? await state.cancelVoice(controller.signal)
      : await state.startVoice(controller.signal);
    if (disposed.current) return;
    updatePolling(nextStatus ?? useAppStore.getState().voiceStatus);
  }, [stopPolling, updatePolling]);

  useEffect(() => {
    let cancelled = false;
    disposed.current = false;
    const controller = new AbortController();
    requestController.current = controller;
    void useAppStore.getState().refreshVoice(controller.signal).then((initialStatus) => {
      if (!cancelled && !disposed.current) updatePolling(initialStatus);
    });
    return () => {
      cancelled = true;
      disposed.current = true;
      stopPolling();
      requestController.current?.abort();
    };
  }, [stopPolling, updatePolling]);

  useEffect(() => {
    if (status?.hotkey?.registered === true) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.ctrlKey
        && event.shiftKey
        && event.code === "Space"
        && !event.repeat
      ) {
        event.preventDefault();
        void toggleVoice();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [status?.hotkey?.registered, toggleVoice]);

  useEffect(() => {
    const refresh = () => {
      const controller = new AbortController();
      requestController.current = controller;
      void useAppStore.getState().refreshVoice(controller.signal).then((nextStatus) => {
        if (!disposed.current) updatePolling(nextStatus);
      });
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [updatePolling]);

  return { status, pending, error, toggleVoice };
}
