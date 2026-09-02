import type { StateCreator } from "zustand";
import { cancelVoiceListening, getVoiceStatus, startVoiceListening } from "../../api/rest";
import type { VoiceStatus } from "../../types/api";

export interface VoiceSlice {
  voiceStatus: VoiceStatus | null;
  voicePending: boolean;
  voiceError: string | null;
  refreshVoice: (signal?: AbortSignal) => Promise<VoiceStatus | null>;
  startVoice: (signal?: AbortSignal) => Promise<VoiceStatus | null>;
  cancelVoice: (signal?: AbortSignal) => Promise<VoiceStatus | null>;
}

export function voiceErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string" && error.length > 0) return error;
  return "语音操作失败";
}

export const createVoiceSlice: StateCreator<VoiceSlice, [], [], VoiceSlice> = (set) => {
  let requestGeneration = 0;
  const run = async (
    request: (signal?: AbortSignal) => Promise<VoiceStatus>,
    signal?: AbortSignal,
    trackPending = false,
  ): Promise<VoiceStatus | null> => {
    const generation = ++requestGeneration;
    if (trackPending) set({ voicePending: true, voiceError: null });
    try {
      const status = await request(signal);
      if (generation !== requestGeneration) return null;
      if (signal?.aborted) {
        if (trackPending) set({ voicePending: false });
        return null;
      }
      set({
        voiceStatus: status,
        voiceError: null,
        ...(trackPending ? { voicePending: false } : {}),
      });
      return status;
    } catch (error) {
      if (generation !== requestGeneration) return null;
      set({
        ...(trackPending ? { voicePending: false } : {}),
        ...(signal?.aborted ? {} : { voiceError: voiceErrorMessage(error) }),
      });
      return null;
    }
  };

  return {
    voiceStatus: null,
    voicePending: false,
    voiceError: null,

    refreshVoice: (signal) => run(getVoiceStatus, signal),
    startVoice: (signal) => run(startVoiceListening, signal, true),
    cancelVoice: (signal) => run(cancelVoiceListening, signal, true),
  };
};
