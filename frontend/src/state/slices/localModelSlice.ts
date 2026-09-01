import type { StateCreator } from "zustand";
import {
  getLocalModelStatus,
  setLocalModelEnabled as requestLocalModelEnabled,
} from "../../api/rest";
import type { LocalModelStatus } from "../../types/api";

export interface LocalModelSlice {
  localModelStatus: LocalModelStatus | null;
  localModelLoading: boolean;
  localModelOperationId: string | null;
  localModelError: string | null;
  trackLocalModelOperation: (operationId: string, error?: string | null) => void;
  finishLocalModelOperation: (error?: string | null) => void;
  refreshLocalModel: (signal?: AbortSignal) => Promise<void>;
  setLocalModelEnabled: (
    enabled: boolean,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<string>;
}

export function localModelErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "本地模型操作失败";
}

export const createLocalModelSlice: StateCreator<
  LocalModelSlice,
  [],
  [],
  LocalModelSlice
> = (set) => ({
  localModelStatus: null,
  localModelLoading: false,
  localModelOperationId: null,
  localModelError: null,

  trackLocalModelOperation: (operationId, error = null) => {
    set({
      localModelLoading: true,
      localModelOperationId: operationId,
      localModelError: error,
    });
  },

  finishLocalModelOperation: (error = null) => {
    set({
      localModelLoading: false,
      localModelOperationId: null,
      localModelError: error,
    });
  },

  refreshLocalModel: async (signal) => {
    set({ localModelLoading: true });
    try {
      const status = await getLocalModelStatus(signal);
      set({ localModelStatus: status, localModelLoading: false, localModelError: null });
    } catch (error) {
      set({ localModelLoading: false, localModelError: localModelErrorMessage(error) });
    }
  },

  setLocalModelEnabled: async (enabled, idempotencyKey, signal) => {
    set({ localModelLoading: true, localModelError: null });
    try {
      const accepted = await requestLocalModelEnabled(enabled, idempotencyKey, signal);
      set({ localModelOperationId: accepted.operation_id });
      return accepted.operation_id;
    } catch (error) {
      set({ localModelLoading: false, localModelError: localModelErrorMessage(error) });
      throw error;
    }
  },
});
